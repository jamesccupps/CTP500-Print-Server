"""Laying out a print from blocks (text, images, QR codes, lists...) into one 384-dot-wide picture.

A document is a dict:

    {"blocks": [{"type": "text", "text": "Hello", "size": 32, "align": "center", "bold": true},
                {"type": "qr", "data": "https://example.com"},
                {"type": "line"}],
     "margin": 6, "gap": 6}

Block types and their options are listed in BLOCK_TYPES; see README.md for the full reference.
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import io
import re

from PIL import Image, ImageDraw, ImageOps

from . import catproto, fonts, raster
from .protocol import WIDTH

NEAREST = getattr(Image, "Resampling", Image).NEAREST
ROTATE_270 = getattr(Image, "Transpose", Image).ROTATE_270

MAX_HEIGHT = 20000          # ~2.5 m of paper; stops a runaway document
try:
    import numpy  # noqa: F401  (the picture styles need it)
    DEFAULT_PICTURE_STYLE = "photo"
except ImportError:
    DEFAULT_PICTURE_STYLE = "floyd"
DOTS_PER_MM = 8
DEFAULT_SIZE = 24
INK, PAPER = 0, 255

BLOCK_TYPES = ("text", "heading", "image", "qr", "line", "space", "row", "list", "checklist", "banner")


class RenderError(ValueError):
    pass


# ---------------------------------------------------------------- placeholders

def expand(text: str, now: _dt.datetime | None = None) -> str:
    """{date} {time} {datetime} {weekday} {year} in any text become the print time."""
    if "{" not in text:
        return text
    now = now or _dt.datetime.now()
    hour = now.strftime("%I").lstrip("0") or "12"
    values = {
        "date": f"{now:%b} {now.day}, {now:%Y}",
        "time": f"{hour}:{now:%M} {now:%p}",
        "weekday": f"{now:%A}",
        "year": f"{now:%Y}",
        "isodate": f"{now:%Y-%m-%d}",
    }
    values["datetime"] = f"{values['date']} {values['time']}"
    return re.sub(r"\{(date|time|datetime|weekday|year|isodate)\}", lambda m: values[m.group(1)], text)


# ---------------------------------------------------------------- text helpers

def _font(block: dict, size: int | None = None, bold: bool | None = None):
    family = str(block.get("font") or "sans")
    return fonts.load(family, int(size or block.get("size") or DEFAULT_SIZE),
                      bool(block.get("bold") if bold is None else bold))


def _px(font) -> int:
    return int(getattr(font, "size", DEFAULT_SIZE))


def _line_height(font, spacing: float) -> int:
    try:
        ascent, descent = font.getmetrics()
        height = ascent + descent
    except AttributeError:
        height = font.getbbox("Ag")[3]
    return max(1, round(height * spacing))


def wrap(text: str, font, width: int) -> list[str]:
    """Word-wrap to a pixel width; words longer than a line are broken by character."""
    lines: list[str] = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        current = ""
        for word in re.split(r"(\s+)", para):
            if not word:
                continue
            candidate = current + word
            if font.getlength(candidate.rstrip()) <= width:
                current = candidate
                continue
            if current.strip():
                lines.append(current.rstrip())
            current = "" if word.isspace() else word
            while current and font.getlength(current) > width:
                cut = len(current)
                while cut > 1 and font.getlength(current[:cut]) > width:
                    cut -= 1
                lines.append(current[:cut])
                current = current[cut:]
        lines.append(current.rstrip())
    return lines


def _draw_lines(draw: ImageDraw.ImageDraw, lines: list[str], font, fake_bold: bool, x0: int, y0: int,
                width: int, align: str, line_h: int, fill: int) -> None:
    stroke = max(1, _px(font) // 22) if fake_bold else 0
    for i, line in enumerate(lines):
        w = font.getlength(line)
        if align == "center":
            x = x0 + (width - w) / 2
        elif align == "right":
            x = x0 + width - w
        else:
            x = x0
        draw.text((x, y0 + i * line_h), line, font=font, fill=fill, stroke_width=stroke, stroke_fill=fill)


def _strip(height: int, color: int = PAPER) -> Image.Image:
    return Image.new("L", (WIDTH, max(1, int(height))), color)


# ---------------------------------------------------------------- blocks

def _text(block: dict, ctx: dict) -> Image.Image:
    text = expand(str(block.get("text", "")), ctx["now"])
    font, fake_bold = _font(block)
    margin = ctx["margin"]
    invert = bool(block.get("invert"))
    pad = int(block.get("padding", 6 if invert else 0))
    width = WIDTH - 2 * margin - 2 * pad
    lines = wrap(text, font, width)
    line_h = _line_height(font, float(block.get("spacing", 1.15)))
    img = _strip(len(lines) * line_h + 2 * pad, INK if invert else PAPER)
    _draw_lines(ImageDraw.Draw(img), lines, font, fake_bold, margin + pad, pad, width,
                str(block.get("align", "left")), line_h, PAPER if invert else INK)
    if block.get("underline"):
        img = _stack([img, _strip(2, INK)], gap=1)
    return img


def _heading(block: dict, ctx: dict) -> Image.Image:
    level = int(block.get("level", 1))
    merged = {"size": {1: 44, 2: 34, 3: 28}.get(level, 28), "bold": True,
              "align": "center" if level == 1 else "left", "spacing": 1.05}
    merged.update(block)
    return _text(merged, ctx)


# Set by the print server: picture -> the picture with its faces sharpened by a face restoration model on the desktop
# PC (GFPGAN, 2026-11-02), or None when the PC is off. Image blocks ask for it with "sharpen_faces".
SHARPEN = None


def _decode_image(block: dict, ctx: dict) -> Image.Image:
    data = block.get("data")
    if data:
        if isinstance(data, str):
            if data.startswith("data:"):
                data = data.split(",", 1)[-1]
            try:
                data = base64.b64decode(data, validate=False)
            except (binascii.Error, ValueError) as exc:
                raise RenderError(f"image data isn't valid base64: {exc}") from None
        try:
            return Image.open(io.BytesIO(data))
        except Exception as exc:
            raise RenderError(f"couldn't read the image: {exc}") from None
    path = block.get("path")
    if path:
        if not ctx["allow_files"]:
            raise RenderError("image 'path' only works from the command line; send 'data' (base64) instead")
        try:
            return Image.open(path)
        except Exception as exc:
            raise RenderError(f"couldn't open {path}: {exc}") from None
    raise RenderError("image block needs 'data' (base64)")


def _image(block: dict, ctx: dict) -> Image.Image:
    # "style" and "dither" are the same option. Unset = the thermal-tuned photo print (2026-10-30: plain dithering
    # prints photos gray and muddy), or plain dithering where numpy isn't installed.
    mode = str(block.get("style") or block.get("dither") or DEFAULT_PICTURE_STYLE)
    if mode not in raster.PICTURE_MODES:
        raise RenderError(f"unknown style '{mode}' (known: {', '.join(raster.PICTURE_MODES)})")
    img = _decode_image(block, ctx)
    if img.format == "JPEG" and max(img.size) > 2048:
        # phone photos are ~12 MP; decode at 1/2 or 1/4 scale straight from the JPEG (much faster on the Pi)
        img.draft("RGB", (img.width * 1600 // max(img.size), img.height * 1600 // max(img.size)))
    img = ImageOps.exif_transpose(img)
    img.load()
    if block.get("sharpen_faces") and SHARPEN is not None:
        img = SHARPEN(img) or img
    if block.get("zoom_faces"):
        img = _zoom_faces(img)
    framed = bool(block.get("frame"))
    border, pad = 3, 8                     # instant-photo frame: line width, gap around the picture
    rotate = int(block.get("rotate", 0))
    options = dict(
        dither=mode,
        brightness=float(block.get("brightness", 1.0)),
        contrast=float(block.get("contrast", 1.0)),
        auto_contrast=bool(block.get("auto_contrast", False)),
        threshold=int(block.get("threshold", 128)),
        rotate=rotate,
        invert=bool(block.get("invert", False)),
        detail=float(block.get("detail", 0.5)),
        weight=int(block.get("weight", 2)),
        level=ctx["level"],
        look=ctx["look"],
    )
    w, h = (img.height, img.width) if rotate % 180 == 90 else img.size
    try:
        if block.get("sideways") and w > h:
            return _sideways(img, w / h, options, framed, str(block.get("caption", "")), ctx, border, pad)
        width = int(block.get("width", WIDTH))
        if framed:
            width = min(width, WIDTH - 2 * (ctx["margin"] + border + pad))
        bw = raster.prepare_image(img, width=width, align="center" if framed else str(block.get("align", "center")),
                                  **options)
    except ImportError:
        raise RenderError(f"the '{mode}' style needs numpy (pip install numpy, or apt install python3-numpy)") from None
    if framed:
        return _frame(bw.convert("L"), width, str(block.get("caption", "")), ctx, border, pad)
    return bw.convert("L")


def _zoom_faces(img: Image.Image) -> Image.Image:
    """Crop to the people (faces.zoom_box); unchanged without faces, numpy or the face model."""
    try:
        from . import faces
        box = faces.zoom_box(img.size, faces.find(img))
    except ImportError:
        return img
    return img.crop(box) if box else img


def _sideways(img: Image.Image, aspect: float, options: dict, framed: bool, caption: str, ctx: dict,
              border: int, pad: int) -> Image.Image:
    """A landscape picture printed along the paper, as big as the paper's width allows (turn the strip to look
    at it, like a banner): laid out landscape, frame and caption included, then turned. ~1.4x the area of an
    upright framed print, ~2x without the frame."""
    margin = ctx["margin"]
    font, fake_bold = fonts.load("sans", 24, False)
    line_h = _line_height(font, 1.1)
    caption = expand(caption, ctx["now"]).strip() if framed else ""
    if framed:
        bottom = 44 if not caption else line_h + 2 * pad + 6
        ph = WIDTH - 2 * margin - border - 2 * pad - bottom
    else:
        bottom, ph = 0, WIDTH - 2 * margin
    pw = max(8, round(ph * aspect))
    picture = raster.render_picture(img, pw, **options).convert("L")
    if picture.height != ph:  # rounding: make it exactly fill its slot
        picture = picture.resize((pw, ph), NEAREST)
    inset = border + pad if framed else 0
    canvas = Image.new("L", (pw + 2 * inset, WIDTH), PAPER)
    canvas.paste(picture, (inset, margin + inset))
    if framed:
        draw = ImageDraw.Draw(canvas)
        for i in range(border):
            draw.rectangle([i, margin + i, canvas.width - 1 - i, WIDTH - 1 - margin - i], outline=INK)
        if caption:
            lines = wrap(caption, font, canvas.width - 2 * inset)[:1]
            top = margin + inset + ph + pad + (bottom - border - line_h) // 2
            _draw_lines(draw, lines, font, fake_bold, 0, top, canvas.width, "center", line_h, INK)
    return canvas.transpose(ROTATE_270)


def _frame(strip: Image.Image, width: int, caption: str, ctx: dict, border: int, pad: int) -> Image.Image:
    """An instant-photo frame around a picture: a thin border, and a deeper bottom with the caption."""
    left = (WIDTH - width) // 2
    picture = strip.crop((left, 0, left + width, strip.height))
    x0, x1 = ctx["margin"], WIDTH - ctx["margin"] - 1
    caption = expand(caption, ctx["now"]).strip()
    font, fake_bold = fonts.load("sans", 24, False)
    lines = wrap(caption, font, x1 - x0 - 2 * (border + pad)) if caption else []
    line_h = _line_height(font, 1.1)
    bottom = max(44, len(lines) * line_h + 2 * pad + 6)
    out = _strip(border + pad + picture.height + pad + bottom)
    out.paste(picture, ((WIDTH - width) // 2, border + pad))
    draw = ImageDraw.Draw(out)
    for i in range(border):
        draw.rectangle([x0 + i, i, x1 - i, out.height - 1 - i], outline=INK)
    if lines:
        top = border + pad + picture.height + pad + (bottom - border - len(lines) * line_h) // 2
        _draw_lines(draw, lines, font, fake_bold, x0, top, x1 - x0, "center", line_h, INK)
    return out


def qr_matrix(data: str, level: str = "M") -> list[list[bool]]:
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q
    except ImportError:
        raise RenderError("QR codes need the 'qrcode' package (pip install qrcode, or apt install python3-qrcode)") from None
    levels = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q, "H": ERROR_CORRECT_H}
    qr = qrcode.QRCode(error_correction=levels.get(level.upper(), ERROR_CORRECT_M), box_size=1, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


def _qr(block: dict, ctx: dict) -> Image.Image:
    data = expand(str(block.get("data", "")), ctx["now"])
    if not data:
        raise RenderError("qr block needs 'data'")
    matrix = qr_matrix(data, str(block.get("level", "M")))
    n = len(matrix)
    quiet = 2
    box = max(1, int(block.get("size", 240)) // (n + 2 * quiet))
    box = min(box, (WIDTH - 2 * ctx["margin"]) // (n + 2 * quiet)) or 1
    code = Image.new("L", (n, n), PAPER)
    code.putdata([INK if cell else PAPER for row in matrix for cell in row])
    side = n * box
    code = code.resize((side, side), NEAREST)
    img = _strip(side + 2 * quiet * box)
    align = str(block.get("align", "center"))
    x = {"left": ctx["margin"], "right": WIDTH - ctx["margin"] - side}.get(align, (WIDTH - side) // 2)
    img.paste(code, (x, quiet * box))
    caption = block.get("caption")
    if caption:
        cap = _text({"text": caption, "size": block.get("caption_size", 20), "align": "center"}, ctx)
        img = _stack([img, cap], gap=0)
    return img


def _line(block: dict, ctx: dict) -> Image.Image:
    thick = max(1, int(block.get("thickness", 2)))
    pad = int(block.get("padding", 6))
    style = str(block.get("style", "solid"))
    img = _strip(thick + 2 * pad)
    draw = ImageDraw.Draw(img)
    x0, x1 = ctx["margin"], WIDTH - ctx["margin"]
    if style == "solid":
        draw.rectangle([x0, pad, x1 - 1, pad + thick - 1], fill=INK)
    else:
        dash, space = (10, 6) if style == "dashed" else (thick, thick * 2)
        x = x0
        while x < x1:
            draw.rectangle([x, pad, min(x + dash, x1) - 1, pad + thick - 1], fill=INK)
            x += dash + space
    return img


def _space(block: dict, ctx: dict) -> Image.Image:
    return _strip(max(1, min(2000, int(block.get("height", 24)))))


def _row(block: dict, ctx: dict) -> Image.Image:
    """Left text and right text on one line (receipts, price lists, key/value)."""
    font, fake_bold = _font(block)
    margin = ctx["margin"]
    left = expand(str(block.get("left", "")), ctx["now"])
    right = expand(str(block.get("right", "")), ctx["now"])
    right_w = int(font.getlength(right)) if right else 0
    gap = 12 if right else 0
    lines = wrap(left, font, WIDTH - 2 * margin - right_w - gap) or [""]
    line_h = _line_height(font, float(block.get("spacing", 1.15)))
    img = _strip(len(lines) * line_h)
    draw = ImageDraw.Draw(img)
    _draw_lines(draw, lines, font, fake_bold, margin, 0, WIDTH - 2 * margin - right_w - gap, "left", line_h, INK)
    if right:
        _draw_lines(draw, [right], font, fake_bold, WIDTH - margin - right_w, 0, right_w, "left", line_h, INK)
    if block.get("leader") and right:  # dotted leader from the end of the text to the price
        last_w = font.getlength(lines[-1])
        y = (len(lines) - 1) * line_h + int(line_h * 0.7)
        x = margin + int(last_w) + 6
        while x < WIDTH - margin - right_w - 6:
            draw.rectangle([x, y, x + 1, y + 1], fill=INK)
            x += 6
    return img


def _items(block: dict) -> list[tuple[str, bool]]:
    out = []
    for item in block.get("items", []):
        if isinstance(item, dict):
            out.append((str(item.get("text", "")), bool(item.get("done"))))
        else:
            out.append((str(item), False))
    return out


def _list(block: dict, ctx: dict, checklist: bool = False) -> Image.Image:
    font, fake_bold = _font(block)
    margin = ctx["margin"]
    line_h = _line_height(font, float(block.get("spacing", 1.2)))
    mark = max(8, int(_px(font) * 0.75)) if checklist else max(4, _px(font) // 4)
    indent = mark + max(8, _px(font) // 2)
    width = WIDTH - 2 * margin - indent
    parts = []
    for text, done in _items(block):
        lines = wrap(expand(text, ctx["now"]), font, width) or [""]
        img = _strip(len(lines) * line_h)
        draw = ImageDraw.Draw(img)
        try:
            ascent = font.getmetrics()[0]
        except AttributeError:
            ascent = line_h
        cy = int(min(ascent * 0.62, line_h / 2))
        if checklist:
            top = max(0, cy - mark // 2)
            box = [margin, top, margin + mark, top + mark]
            draw.rectangle(box, outline=INK, width=max(2, mark // 10))
            if done:
                w = max(2, mark // 8)
                draw.line([margin + mark * 0.2, top + mark * 0.55, margin + mark * 0.45, top + mark * 0.8,
                           margin + mark * 0.85, top + mark * 0.2], fill=INK, width=w)
        else:
            r = mark / 2
            draw.ellipse([margin, cy - r, margin + mark, cy + r], fill=INK)
        _draw_lines(draw, lines, font, fake_bold, margin + indent, 0, width, "left", line_h, INK)
        if checklist and done and block.get("strike", True):
            for i, line in enumerate(lines):
                y = i * line_h + cy
                draw.line([margin + indent, y, margin + indent + font.getlength(line), y], fill=INK, width=2)
        parts.append(img)
    return _stack(parts, gap=int(block.get("item_gap", 2))) if parts else _strip(1)


def _banner(block: dict, ctx: dict) -> Image.Image:
    """Huge text that runs along the paper (turn the strip sideways to read it)."""
    text = expand(str(block.get("text", "")), ctx["now"]).replace("\n", " ").strip()
    if not text:
        raise RenderError("banner block needs 'text'")
    family = str(block.get("font") or "sans")
    bold = bool(block.get("bold", True))
    avail = WIDTH - 2 * ctx["margin"]
    lo, hi = 20, 400
    while lo < hi:  # the biggest size whose ink fits across the paper
        mid = (lo + hi + 1) // 2
        font, _ = fonts.load(family, mid, bold)
        top, bottom = font.getbbox(text)[1], font.getbbox(text)[3]
        if bottom - top <= avail:
            lo = mid
        else:
            hi = mid - 1
    font, fake_bold = fonts.load(family, lo, bold)
    box = font.getbbox(text)
    w, h = box[2] - box[0], box[3] - box[1]
    pad = 16
    canvas = Image.new("L", (w + 2 * pad, WIDTH), PAPER)
    draw = ImageDraw.Draw(canvas)
    y = (WIDTH - h) // 2 - box[1]
    stroke = max(1, lo // 22) if fake_bold else 0
    draw.text((pad - box[0], y), text, font=font, fill=INK, stroke_width=stroke, stroke_fill=INK)
    return canvas.transpose(ROTATE_270)


RENDERERS = {
    "text": _text,
    "heading": _heading,
    "image": _image,
    "qr": _qr,
    "line": _line,
    "space": _space,
    "row": _row,
    "list": _list,
    "checklist": lambda b, c: _list(b, c, checklist=True),
    "banner": _banner,
}


def _stack(parts: list[Image.Image], gap: int) -> Image.Image:
    height = sum(p.height for p in parts) + gap * max(0, len(parts) - 1)
    if height > MAX_HEIGHT:
        raise RenderError(f"print is {height} dots long; the limit is {MAX_HEIGHT} (about 2.5 m)")
    out = _strip(height)
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.height + gap
    return out


def normalize(doc) -> dict:
    """Accept the shorthands: a plain string, {"text": ...}, {"markup": ...} or a list of blocks."""
    from . import markup

    if isinstance(doc, str):
        return {"blocks": markup.parse(doc)}
    if isinstance(doc, list):
        doc = {"blocks": doc}
    if not isinstance(doc, dict):
        raise RenderError("a print is a JSON object with 'blocks', 'text' or 'markup'")
    doc = dict(doc)
    if "blocks" not in doc:
        if "markup" in doc:
            doc["blocks"] = [{"type": "markup", "markup": doc["markup"], "size": doc.get("size", DEFAULT_SIZE),
                              "font": doc.get("font", "sans")}]
        elif "text" in doc:
            doc["blocks"] = [{"type": "text", "text": doc["text"], "size": doc.get("size", DEFAULT_SIZE),
                              "align": doc.get("align", "left"), "bold": doc.get("bold", False),
                              "font": doc.get("font", "sans")}]
        else:
            raise RenderError("nothing to print: give 'blocks', 'text' or 'markup'")
    if not isinstance(doc["blocks"], list):
        raise RenderError("'blocks' must be a list")
    # {"type": "markup"} blocks hold note markup; expand them in place
    blocks = []
    for block in doc["blocks"]:
        if isinstance(block, dict) and block.get("type") == "markup":
            blocks.extend(markup.parse(str(block.get("markup", "")), size=int(block.get("size", DEFAULT_SIZE)),
                                       font=str(block.get("font", "sans"))))
        else:
            blocks.append(block)
    doc["blocks"] = blocks
    return doc


def _mm_to_dots(mm) -> int:
    return round(max(0.0, min(100.0, float(mm or 0))) * DOTS_PER_MM)


# picture styles that print as photos (dithered tones) rather than lines and solid shapes
PHOTO_MODES = ("photo", "photoline", "floyd", "atkinson")
PHOTO_LOOKS = ("punchy", "even")


def has_photo(doc: dict) -> bool:
    return any(isinstance(b, dict) and b.get("type") == "image"
               and str(b.get("style") or b.get("dither") or DEFAULT_PICTURE_STYLE) in PHOTO_MODES
               for b in doc.get("blocks", []))


def with_defaults(doc: dict, cfg: dict) -> dict:
    """Fill in what the settings decide, unless the document sets it: the blank paper above and below, the photo
    look (photo_look: "punchy", or "even" = the measured tones, styles.thermal_photo) and the darkness: "even"
    photos print at light (the printer keeps far more tones apart there and black is just as black,
    styles.PRINTED), everything else at darkness. A darkness of "auto" means the same as none."""
    for key in ("padding_top_mm", "padding_bottom_mm"):
        if key not in doc and cfg.get(key) is not None:
            doc[key] = cfg[key]
    photo = has_photo(doc)
    if photo and doc.get("photo_look") not in PHOTO_LOOKS:
        doc["photo_look"] = cfg.get("photo_look") if cfg.get("photo_look") in PHOTO_LOOKS else "punchy"
    if doc.get("darkness") in (None, "", "auto"):
        value = "light" if photo and doc["photo_look"] == "even" else cfg.get("darkness")
        if value:
            doc["darkness"] = value
        else:
            doc.pop("darkness", None)
    return doc


with_padding = with_defaults   # its older name


def render(doc, allow_files: bool = False, now: _dt.datetime | None = None) -> Image.Image:
    """Document -> 1-bit full-width image, ready for raster.pack()."""
    doc = normalize(doc)
    try:   # the photo styles' tones depend on how dark it prints
        level = catproto.darkness_setting(None if doc.get("darkness") == "auto" else doc.get("darkness"))[0]
    except (TypeError, ValueError):
        level = catproto.DEFAULT_DARKNESS[0]
    ctx = {"margin": max(0, min(80, int(doc.get("margin", 6)))), "allow_files": allow_files,
           "now": now or _dt.datetime.now(), "level": level,
           "look": doc.get("photo_look") if doc.get("photo_look") in PHOTO_LOOKS else "punchy"}
    parts = []
    for i, block in enumerate(doc["blocks"]):
        if not isinstance(block, dict):
            block = {"type": "text", "text": str(block)}
        kind = str(block.get("type", "text"))
        fn = RENDERERS.get(kind)
        if fn is None:
            raise RenderError(f"block {i + 1}: unknown type '{kind}' (known: {', '.join(BLOCK_TYPES)})")
        try:
            parts.append(fn(block, ctx))
        except RenderError as exc:
            raise RenderError(f"block {i + 1} ({kind}): {exc}") from None
        except (TypeError, ValueError) as exc:
            raise RenderError(f"block {i + 1} ({kind}): bad option ({exc})") from None
    if not parts:
        raise RenderError("nothing to print")
    page = _stack(parts, gap=max(0, int(doc.get("gap", 6))))
    top, bottom = (_mm_to_dots(doc.get(key, 0)) for key in ("padding_top_mm", "padding_bottom_mm"))
    if top or bottom:
        page = _stack([p for p in (_strip(top) if top else None, page, _strip(bottom) if bottom else None)
                       if p is not None], gap=0)
    # Anti-aliased text edges go dark rather than vanish: thermal dots look thin otherwise.
    return page.point(lambda v: 255 if v > 170 else 0).convert("1", dither=raster.NO_DITHER)
