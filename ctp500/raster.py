"""Turning pictures into the printer's 1-bit rows (384 dots on the CTP500, 576 on the M02 Pro)."""

from __future__ import annotations

from PIL import Image, ImageEnhance, ImageOps

from .protocol import WIDTH

LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
FLOYD = getattr(Image, "Dither", Image).FLOYDSTEINBERG
NO_DITHER = getattr(Image, "Dither", Image).NONE

DITHER_MODES = ("floyd", "atkinson", "threshold")
STYLE_NAMES = ("photo", "photoline", "line", "artist", "sketch", "coloring", "poster", "halftone",
               "hatch")  # styles.STYLES
PICTURE_MODES = DITHER_MODES + STYLE_NAMES
# drawn styles (lines, stripes, dots): on finer paper they're drawn at the CTP500 size and scaled up
DRAWN = ("line", "artist", "coloring", "poster", "halftone", "hatch")

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


def fit_width(img: Image.Image, width: int, paper_width: int = WIDTH) -> Image.Image:
    width = max(8, min(paper_width, int(width)))
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


def clustered_atkinson(gray: Image.Image, clumps: float = 0.6) -> Image.Image:
    """Atkinson dithering whose dots come in clumps (Levien's output-dependent feedback, "green noise"): the
    four neighbours already decided (behind in this row, the three above) pull each dot towards their color by
    clumps x 127.5. The error is taken from the value before the pull, so the tones stay as they were; rows run
    alternately left and right, or the clumps line up into diagonal streaks.

    For the M02 Pro's 300 dpi (2026-11-11): its lone dots print weakly, so plain dithering gave blotchy photos,
    and 2x2 dots lost the detail. clumps 0.6 left ~0.5% of the dots alone (plain Atkinson: 5-20%) and looked
    best on paper (over 0.35)."""
    w, h = gray.size
    px = [float(v) for v in gray.convert("L").tobytes()]
    out = bytearray(w * h)
    pull = clumps * 127.5 / 4
    for y in range(h):
        row = y * w
        d = 1 if y % 2 == 0 else -1
        for x in (range(w) if d == 1 else range(w - 1, -1, -1)):
            i = row + x
            old = px[i]
            fb = (1.0 if out[i - d] else -1.0) if 0 <= x - d < w else 1.0      # paper outside counts as white
            if y > 0:
                up = i - w
                fb += 1.0 if out[up] else -1.0
                fb += (1.0 if out[up - 1] else -1.0) if x > 0 else 1.0
                fb += (1.0 if out[up + 1] else -1.0) if x + 1 < w else 1.0
            else:
                fb += 3.0
            new = 255 if old + pull * fb >= 128 else 0
            out[i] = new
            err = (old - new) / 8.0
            if err == 0:
                continue
            if 0 <= x + d < w:
                px[i + d] += err
            if 0 <= x + 2 * d < w:
                px[i + 2 * d] += err
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


def place(bw: Image.Image, align: str = "center", paper_width: int = WIDTH) -> Image.Image:
    """Put a 1-bit image on a full-width strip of paper."""
    if bw.width == paper_width:
        return bw
    strip = Image.new("1", (paper_width, bw.height), 1)
    x = {"left": 0, "right": paper_width - bw.width}.get(align, (paper_width - bw.width) // 2)
    strip.paste(bw, (x, 0))
    return strip


def pack(bw: Image.Image, paper_width: int = WIDTH) -> bytes:
    """Rows for GS v 0: paper_width / 8 bytes per row (48 on the CTP500), MSB is the leftmost dot, 1 = black."""
    if bw.mode != "1":
        bw = bw.convert("1", dither=NO_DITHER)
    if bw.width != paper_width:
        bw = place(bw, paper_width=paper_width) if bw.width < paper_width else bw.crop((0, 0, paper_width, bw.height))
    data = bw.tobytes()  # mode "1" packs MSB-first with 1 = white
    assert len(data) == (paper_width + 7) // 8 * bw.height
    return data.translate(_INVERT)


def unpack(rows: bytes, height: int, paper_width: int = WIDTH) -> Image.Image:
    """The inverse of pack(), for previews and the simulated printer."""
    return Image.frombytes("1", (paper_width, height), rows.translate(_INVERT))


def render_picture(img: Image.Image, width: int, dither: str = "floyd", brightness: float = 1.0,
                   contrast: float = 1.0, auto_contrast: bool = False, threshold: int = 128, rotate: int = 0,
                   invert: bool = False, detail: float = 0.5, weight: int = 2, level: int = 3,
                   look: str = "punchy", scale: float = 1.0) -> Image.Image:
    """Any picture to a 1-bit image exactly `width` dots wide, any width (sideways prints are wider than the
    paper before they're turned). dither is a dither mode or a picture style (styles.STYLES); look and level (the
    darkness level it prints at, catproto) set the photo styles' tones. scale: the paper's dots per CTP500 dot
    (printers.Paper.scale): the styles' line widths and sizes were tuned in CTP500 dots."""
    width = max(8, int(width))
    if dither in DRAWN and abs(scale - 1.0) > 0.01:
        # lines, stripes and dots: drawn at the CTP500's size, then scaled up smoothly (they keep their tuned
        # weights and come out the same size on paper, with finer edges)
        small = render_picture(img, max(8, round(width / scale)), dither, brightness, contrast, auto_contrast,
                               threshold, rotate, invert, detail, weight, level, look)
        height = max(1, round(small.height * width / small.width))
        return small.convert("L").resize((width, height), LANCZOS).point(
            lambda v: 255 if v >= 128 else 0).convert("1", dither=NO_DITHER)
    img = ImageOps.exif_transpose(img)
    if dither in STYLE_NAMES:
        from . import styles  # needs numpy; plain dithering doesn't
        if styles.wants_rgb(dither):
            # AI and thermal-photo styles want the full-color picture, at more than print size
            rgb = flatten_rgb(img)
            if rotate % 360:
                rgb = rgb.rotate(-rotate, expand=True, fillcolor=(255, 255, 255))
            rgb = adjust(rgb, brightness, contrast, auto_contrast)
            bw = styles.apply_rgb(rgb, dither, width, detail, weight, level, look, scale)
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
        bw = styles.apply(gray, dither, detail, weight, scale)
        if invert:
            bw = ImageOps.invert(bw.convert("L")).convert("1", dither=NO_DITHER)
        return bw
    if invert:
        gray = ImageOps.invert(gray)
    return to_1bit(gray, dither, threshold)


def prepare_image(img: Image.Image, width: int = WIDTH, dither: str = "floyd", align: str = "center",
                  brightness: float = 1.0, contrast: float = 1.0, auto_contrast: bool = False,
                  threshold: int = 128, rotate: int = 0, invert: bool = False, detail: float = 0.5,
                  weight: int = 2, level: int = 3, look: str = "punchy", paper_width: int = WIDTH,
                  scale: float = 1.0) -> Image.Image:
    """Any picture to a print-ready, full-width 1-bit strip (at most the paper's width)."""
    bw = render_picture(img, max(8, min(paper_width, int(width))), dither, brightness, contrast, auto_contrast,
                        threshold, rotate, invert, detail, weight, level, look, scale)
    return place(bw, align, paper_width)
