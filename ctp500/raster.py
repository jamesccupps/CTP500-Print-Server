"""Turning pictures into the printer's 1-bit, 384-dot rows."""

from __future__ import annotations

from PIL import Image, ImageEnhance, ImageOps

from .protocol import ROW_BYTES, WIDTH

LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
FLOYD = getattr(Image, "Dither", Image).FLOYDSTEINBERG
NO_DITHER = getattr(Image, "Dither", Image).NONE

DITHER_MODES = ("floyd", "atkinson", "threshold")
STYLE_NAMES = ("photo", "photoline", "line", "artist", "sketch", "coloring", "poster", "halftone",
               "hatch")  # styles.STYLES
PICTURE_MODES = DITHER_MODES + STYLE_NAMES

_INVERT = bytes(255 - i for i in range(256))


def flatten_rgb(img: Image.Image) -> Image.Image:
    """Color on white, whatever the source mode (transparent areas print as paper)."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(white, rgba).convert("RGB")
    return img.convert("RGB")


def flatten(img: Image.Image) -> Image.Image:
    """Grayscale on white, whatever the source mode (transparent areas print as paper)."""
    return flatten_rgb(img).convert("L")


def fit_width(img: Image.Image, width: int) -> Image.Image:
    width = max(8, min(WIDTH, int(width)))
    if img.width == width:
        return img
    height = max(1, round(img.height * width / img.width))
    return img.resize((width, height), LANCZOS)


def adjust(gray: Image.Image, brightness: float = 1.0, contrast: float = 1.0,
           auto_contrast: bool = False) -> Image.Image:
    if auto_contrast:
        gray = ImageOps.autocontrast(gray, cutoff=1)
    if contrast != 1.0:
        gray = ImageEnhance.Contrast(gray).enhance(contrast)
    if brightness != 1.0:
        gray = ImageEnhance.Brightness(gray).enhance(brightness)
    return gray


def to_1bit(gray: Image.Image, mode: str = "floyd", threshold: int = 128) -> Image.Image:
    """Mode "1" image (0 = black dot, 255 = paper)."""
    gray = gray.convert("L")
    if mode == "threshold":
        return gray.point(lambda v: 255 if v >= threshold else 0).convert("1", dither=NO_DITHER)
    if mode == "atkinson":
        return _atkinson(gray)
    return gray.convert("1", dither=FLOYD)


def _atkinson(gray: Image.Image) -> Image.Image:
    """Atkinson dithering: spreads only 3/4 of the error, so highlights stay clean.
    Thermal paper loves it for photos."""
    w, h = gray.size
    px = [float(v) for v in gray.tobytes()]
    out = bytearray(w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            old = px[i]
            new = 255 if old >= 128 else 0
            out[i] = new
            err = (old - new) / 8.0
            if err == 0:
                continue
            if x + 1 < w:
                px[i + 1] += err
            if x + 2 < w:
                px[i + 2] += err
            if y + 1 < h:
                below = i + w
                if x > 0:
                    px[below - 1] += err
                px[below] += err
                if x + 1 < w:
                    px[below + 1] += err
                if y + 2 < h:
                    px[below + w] += err
    return Image.frombytes("L", (w, h), bytes(out)).convert("1", dither=NO_DITHER)


def place(bw: Image.Image, align: str = "center") -> Image.Image:
    """Put a 1-bit image on a full-width strip of paper."""
    if bw.width == WIDTH:
        return bw
    strip = Image.new("1", (WIDTH, bw.height), 1)
    x = {"left": 0, "right": WIDTH - bw.width}.get(align, (WIDTH - bw.width) // 2)
    strip.paste(bw, (x, 0))
    return strip


def pack(bw: Image.Image) -> bytes:
    """Rows for GS v 0: 48 bytes per row, MSB is the leftmost dot, 1 = black."""
    if bw.mode != "1":
        bw = bw.convert("1", dither=NO_DITHER)
    if bw.width != WIDTH:
        bw = place(bw)
    data = bw.tobytes()  # mode "1" packs MSB-first with 1 = white
    assert len(data) == ROW_BYTES * bw.height
    return data.translate(_INVERT)


def unpack(rows: bytes, height: int) -> Image.Image:
    """The inverse of pack(), for previews and the simulated printer."""
    return Image.frombytes("1", (WIDTH, height), rows.translate(_INVERT))


def render_picture(img: Image.Image, width: int, dither: str = "floyd", brightness: float = 1.0,
                   contrast: float = 1.0, auto_contrast: bool = False, threshold: int = 128, rotate: int = 0,
                   invert: bool = False, detail: float = 0.5, weight: int = 2, level: int = 3,
                   look: str = "punchy") -> Image.Image:
    """Any picture to a 1-bit image exactly `width` dots wide, any width (sideways prints are wider than the
    paper before they're turned). dither is a dither mode or a picture style (styles.STYLES); look and level (the
    darkness level it prints at, catproto) set the photo styles' tones."""
    width = max(8, int(width))
    img = ImageOps.exif_transpose(img)
    if dither in STYLE_NAMES:
        from . import styles  # needs numpy; plain dithering doesn't
        if styles.wants_rgb(dither):
            # AI and thermal-photo styles want the full-color picture, at more than print size
            rgb = flatten_rgb(img)
            if rotate % 360:
                rgb = rgb.rotate(-rotate, expand=True, fillcolor=(255, 255, 255))
            rgb = adjust(rgb, brightness, contrast, auto_contrast)
            bw = styles.apply_rgb(rgb, dither, width, detail, weight, level, look)
            if invert:
                bw = ImageOps.invert(bw.convert("L")).convert("1", dither=NO_DITHER)
            return bw
    gray = flatten(img)
    if rotate % 360:
        gray = gray.rotate(-rotate, expand=True, fillcolor=255)
    if gray.width != width:
        gray = gray.resize((width, max(1, round(gray.height * width / gray.width))), LANCZOS)
    gray = adjust(gray, brightness, contrast, auto_contrast)
    if dither in STYLE_NAMES:
        bw = styles.apply(gray, dither, detail, weight)
        if invert:
            bw = ImageOps.invert(bw.convert("L")).convert("1", dither=NO_DITHER)
        return bw
    if invert:
        gray = ImageOps.invert(gray)
    return to_1bit(gray, dither, threshold)


def prepare_image(img: Image.Image, width: int = WIDTH, dither: str = "floyd", align: str = "center",
                  brightness: float = 1.0, contrast: float = 1.0, auto_contrast: bool = False,
                  threshold: int = 128, rotate: int = 0, invert: bool = False, detail: float = 0.5,
                  weight: int = 2, level: int = 3, look: str = "punchy") -> Image.Image:
    """Any picture to a print-ready, full-width 1-bit strip (at most the paper's width)."""
    bw = render_picture(img, max(8, min(WIDTH, int(width))), dither, brightness, contrast, auto_contrast,
                        threshold, rotate, invert, detail, weight, level, look)
    return place(bw, align)
