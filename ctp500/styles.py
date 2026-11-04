"""Picture styles that look drawn rather than photographed: line drawing, pencil sketch, coloring page,
poster, halftone and hatching.

Every style takes a grayscale picture already sized for the paper (so line widths are in printer dots) and
returns a mode "1" image (0 = ink). Needs numpy (python3-numpy on the Pi).

    detail  0..1  how much fine structure survives (0 = only the big shapes)
    weight  1..3  line thickness: thin, medium, bold
"""

from __future__ import annotations

import functools

import numpy as np
from PIL import Image, ImageFilter, ImageOps

STYLES = ("photo", "photoline", "line", "artist", "sketch", "coloring", "poster", "halftone", "hatch")

# Styles drawn by an AI model when it's installed (ai.available()); they fall back to edge detection.
AI_STYLES = {"line": "lineart", "artist": "opensketch", "coloring": "lineart"}
# Photo styles tuned for thermal paper; they work from the full-color picture too.
PHOTO_STYLES = ("photo", "photoline")


def uses_ai(style: str) -> bool:
    if style not in AI_STYLES:
        return False
    from . import ai
    return ai.available(AI_STYLES[style])


def wants_rgb(style: str) -> bool:
    """Styles that work from the full-size color picture rather than the paper-sized grayscale one."""
    return style in PHOTO_STYLES or uses_ai(style)


def apply_rgb(rgb: Image.Image, style: str, width: int, detail: float = 0.5, weight: int = 2,
              level: int = 3, look: str = "punchy") -> Image.Image:
    if style in PHOTO_STYLES:
        return thermal_photo(rgb, width, detail, weight, outlines=(style == "photoline"), level=level, look=look)
    return apply_ai(rgb, style, width, detail, weight)


# How bright flat patches print (share of the paper's brightness) at 0, 10, ..., 100% ink, per darkness level
# (catproto: 2 light, 3 normal, 4 dark, 5 darkest). Measured 2026-10-30 on a CTP500 (cat protocol) from a phone photo of
# tools/calibration_strip.py. Solid black is the same at every level, but from normal up the dots spread so much
# that everything past ~60% ink prints as that black, while light keeps the steps apart. Used by the "even" photo
# look (config photo_look), which prints at light.
PRINTED = {
    2: (1.00, 0.98, 0.91, 0.78, 0.63, 0.45, 0.35, 0.23, 0.16, 0.13, 0.12),
    3: (1.00, 0.96, 0.82, 0.64, 0.47, 0.29, 0.23, 0.16, 0.13, 0.12, 0.12),
    4: (1.00, 0.96, 0.82, 0.64, 0.47, 0.29, 0.24, 0.18, 0.14, 0.13, 0.12),
    5: (1.00, 0.94, 0.80, 0.62, 0.44, 0.26, 0.21, 0.17, 0.15, 0.13, 0.13),
}


def _lstar(y: np.ndarray) -> np.ndarray:
    return np.where(y > 0.008856, 116 * np.cbrt(y) - 16, 903.3 * y)


@functools.lru_cache(maxsize=16)
def tone_curve(level: int = 2, lift: float = 1.0) -> tuple[int, ...]:
    """Gray as the photo should look (0..255) -> gray to hand Atkinson, so the paper shows the photo's lightness
    spread evenly (in L*) between solid black and bare paper. Undoes the measured dot gain (PRINTED) and Atkinson's
    own response on flat areas (it keeps 3/4 of the error, so coverage ~ 0.5 + 1.27 (ink - 0.5): nothing under
    ~10% ink, solid over ~90%). Replaced a guessed gamma 0.8 (2026-10-30) that printed everything lighter than
    gray 192 (light skin, white shirts) as bare paper and merged the darkest quarter into black. lift < 1
    brightens the photo a little (under outlines)."""
    printed = PRINTED.get(min(5, max(2, int(level))))
    printed = np.minimum.accumulate(np.array(printed, dtype=np.float64)) - np.arange(len(printed)) * 1e-6
    ink = np.linspace(0.0, 1.0, len(printed))
    v = np.arange(256) / 255.0
    lin = np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)
    black = float(_lstar(np.array(printed[-1])))
    target = black + (100.0 - black) * (_lstar(lin) / 100.0) ** lift
    reflect = np.where(target > 8, ((target + 16) / 116) ** 3, target / 903.3)
    cover = np.interp(-reflect, -printed, ink)                 # printed brightness falls as ink rises
    atk = np.clip(0.5 + (cover - 0.5) / 1.27, 0.0, 1.0)
    atk = np.where(cover <= 0.002, 0.0, np.where(cover >= 0.998, 1.0, atk))   # paper stays paper, black black
    return tuple(int(round(255 * (1 - a))) for a in atk)


def thermal_photo(rgb: Image.Image, width: int, detail: float = 0.5, weight: int = 2,
                  outlines: bool = False, level: int = 3, look: str = "punchy") -> Image.Image:
    """A photo prepared for thermal paper, which plain dithering prints gray and muddy (2026-10-30, MEMENTO camera shots):
    calm the sensor grain, dehaze as much as the picture needs, stretch contrast, size it, sharpen (detail sets
    how much), set the tones, then Atkinson dithering, which keeps light areas clean. With outlines, a thin AI
    outline is laid over it so faces stay defined; weight sets how much outline (thin / medium / bold = 5 / 8 / 11%
    of the picture).

    look "punchy" (the default): a midtone lift (gamma 0.8, 0.7 under outlines), printed at normal. Light skin goes
    to bare paper and deep shadows to black, but it beat "even" in an A/B print (2026-10-30), above
    all for dim shots. look "even": the measured tone_curve for this darkness level, printed at light: more gray
    steps, flatter."""
    from . import ai
    detail = min(1.0, max(0.0, float(detail)))
    weight = min(3, max(1, int(weight)))
    width = max(16, int(width))
    work = rgb.convert("RGB").filter(ImageFilter.GaussianBlur(max(0.3, rgb.width / 1600)))
    work = ai.local_contrast(work, min(0.55, ai.dehaze_strength(work)))
    gray = ImageOps.autocontrast(work.convert("L"), cutoff=1)
    if FACE_TONES:
        gray = _face_tones(gray, rgb)
    gray = gray.resize((width, max(1, round(gray.height * width / gray.width))), Image.LANCZOS)
    gray = gray.filter(ImageFilter.UnsharpMask(radius=1.6, percent=round(60 + 100 * detail), threshold=2))
    if look == "even":
        gray = gray.point(list(tone_curve(level, 0.9 if outlines else 1.0)))
    else:
        gamma = 0.7 if outlines else 0.8
        gray = gray.point([round(255 * (i / 255) ** gamma) for i in range(256)])
    from .raster import to_1bit
    ink = np.asarray(to_1bit(gray, "atkinson").convert("L")) < 128
    if outlines and ai.available("lineart"):
        drawing = ai.draw(rgb, "lineart", max(256, round(width * 1.67)))
        tones = np.asarray(drawing.resize((width, ink.shape[0]), Image.LANCZOS))
        share = {1: 0.05, 2: 0.08, 3: 0.11}[weight]
        lines = _remove_specks(tones < float(min(200, np.quantile(tones, share))), 8)
        ink = ink | lines
    return _to_1bit(ink)


# Tones set for the faces rather than the whole room (2026-10-31): after the stretch, the faces of the 55 test
# shots sat anywhere from 0.11 (in shadow) to 0.66; the prints that looked right had them at ~0.6. Faces outside
# FACE_RANGE are brought to its nearer end by a gamma on the whole picture, capped so the room isn't washed out.
FACE_TONES = True
FACE_RANGE = (0.5, 0.68)


def _face_tones(gray: Image.Image, rgb: Image.Image) -> Image.Image:
    from . import faces
    found = faces.find(rgb)
    if not found:
        return gray
    a = np.asarray(gray, dtype=np.float32) / 255.0
    s = gray.width / rgb.width
    total = weight = 0.0
    for f in found:
        l, t, r, b = f.box(0.7)                     # the inner face: skin, eyes, mouth, no hair
        l, t = max(0, round(l * s)), max(0, round(t * s))
        r, b = min(a.shape[1], round(r * s)), min(a.shape[0], round(b * s))
        if r - l < 2 or b - t < 2:
            continue
        area = (r - l) * (b - t)                    # bigger faces count more
        total += float(a[t:b, l:r].mean()) * area
        weight += area
    if not weight:
        return gray
    m = min(0.98, max(0.02, total / weight))
    low, high = FACE_RANGE
    if low <= m <= high:
        return gray
    gamma = min(1.25, max(0.6, np.log(low if m < low else high) / np.log(m)))
    return gray.point([round(255 * (i / 255) ** gamma) for i in range(256)])


def apply_ai(rgb: Image.Image, style: str, width: int, detail: float = 0.5, weight: int = 2) -> Image.Image:
    """AI-drawn styles, from the full-color picture. The model runs a bit larger than the print and is scaled
    down, which keeps strokes smooth; weight picks that size (a smaller run = relatively bolder lines) and
    detail the threshold (how faint a line still prints). Tuned 2026-10-30 on the MEMENTO camera's indoor shots."""
    from . import ai
    detail = min(1.0, max(0.0, float(detail)))
    weight = min(3, max(1, int(weight)))
    width = max(16, int(width))
    if style == "coloring":
        factor, threshold, specks = 1.0, 195 + 30 * detail, 30
    else:
        # the model runs larger than the print (cleaner faces at x1.67 than x1.33, 2026-10-30), then scales down
        factor, threshold, specks = (1.67 if weight < 3 else 1.33), 190 + 45 * detail, 6
    drawing = ai.draw(rgb, AI_STYLES[style], max(256, round(width * factor)))
    small = drawing.resize((width, max(1, round(drawing.height * width / drawing.width))), Image.LANCZOS)
    tones = np.asarray(small)
    ink = tones < threshold
    # A blurry or murky photo makes the model shade instead of draw; keep only its strongest marks then. Good
    # photos come out at ~11-12% ink and are untouched.
    cap = 0.16 if style != "coloring" else 0.20
    if ink.mean() > cap:
        ink = tones < float(np.quantile(tones, cap))
    ink = _remove_specks(ink, specks)
    if style == "coloring":
        ink = _thicken(ink, 2 if weight < 3 else 3)
    elif weight == 2:           # 1-dot lines print faint: grow them one dot right and down
        grown = ink.copy()
        grown[:, 1:] |= ink[:, :-1]
        grown[1:, :] |= ink[:-1, :]
        ink = grown
    elif weight == 3:
        ink = _thicken(ink, 2)
    return _to_1bit(ink)


NO_DITHER = getattr(Image, "Dither", Image).NONE
FLOYD = getattr(Image, "Dither", Image).FLOYDSTEINBERG


def apply(gray: Image.Image, style: str, detail: float = 0.5, weight: int = 2) -> Image.Image:
    detail = min(1.0, max(0.0, float(detail)))
    weight = min(3, max(1, int(weight)))
    if style in PHOTO_STYLES:   # normally routed through apply_rgb; this is only a safety net
        return thermal_photo(gray.convert("RGB"), gray.width, detail, weight, outlines=False)
    fn = {"line": line, "artist": line, "sketch": sketch, "coloring": coloring, "poster": poster,
          "halftone": halftone, "hatch": hatch}[style]
    return fn(gray.convert("L"), detail, weight)


# ---------------------------------------------------------------- helpers

def _f(img: Image.Image) -> np.ndarray:
    return np.asarray(img, dtype=np.float32) / 255.0


def _blur(img: Image.Image, sigma: float) -> np.ndarray:
    return _f(img.filter(ImageFilter.GaussianBlur(sigma)))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _to_1bit(ink: np.ndarray) -> Image.Image:
    """Boolean ink mask -> mode "1" (ink = 0)."""
    return Image.fromarray(np.where(ink, 0, 255).astype(np.uint8), "L").convert("1", dither=NO_DITHER)


def _prepare(gray: Image.Image, median: int = 3) -> Image.Image:
    """Stretch the contrast so soft photos still give lines, and calm the noise a little."""
    img = ImageOps.autocontrast(gray, cutoff=1)
    return img.filter(ImageFilter.MedianFilter(median)) if median > 1 else img


def _edges(gray: Image.Image, sigma: float) -> np.ndarray:
    """Difference of Gaussians: > 0 where a pixel is darker than its surroundings, i.e. on the dark side of
    an edge, which is where an artist puts the line."""
    return _blur(gray, sigma * 1.6) - _blur(gray, sigma)


def _dog_lines(gray: Image.Image, sigma: float, amount: float, floor: float = 0.006) -> np.ndarray:
    """amount is the share of the picture that may become line (the strongest edges win), so every photo
    gets a similar amount of drawing whatever its contrast; floor keeps flat areas clean."""
    edge = _edges(gray, sigma)
    return edge > max(floor, float(np.quantile(edge, 1.0 - amount)))


def _strokes(gray: Image.Image, sigma: float, amount: float, min_size: int) -> np.ndarray:
    """Lines with hysteresis, like a Canny edge detector: only strong edges start a line, and weaker edges
    survive only where they connect to one. Gives long, continuous strokes and no stray texture dashes."""
    edge = _edges(gray, sigma)
    weak = edge > max(0.004, float(np.quantile(edge, 1.0 - amount)))
    strong = edge > max(0.01, float(np.quantile(edge, 1.0 - amount / 3)))
    return _components(weak, min_size, seeds=strong)


def _thicken(ink: np.ndarray, weight: int) -> np.ndarray:
    if weight <= 1:
        return ink
    img = Image.fromarray(np.where(ink, 0, 255).astype(np.uint8), "L")
    img = img.filter(ImageFilter.MinFilter(3 if weight == 2 else 5))
    return np.asarray(img) < 128


def _remove_specks(ink: np.ndarray, min_size: int) -> np.ndarray:
    """Drop ink blobs smaller than min_size pixels (8-connected)."""
    return ink if min_size <= 1 else _components(ink, min_size)


def _components(ink: np.ndarray, min_size: int, seeds: np.ndarray | None = None) -> np.ndarray:
    """Keep the 8-connected ink blobs of at least min_size pixels (and, with seeds, only those touching a seed
    pixel). Plain Python flood fill over the ink pixels only; line art has few, so it's quick on a Pi."""
    h, w = ink.shape
    flat = ink.ravel().copy()
    seed_flat = seeds.ravel() if seeds is not None else None
    seen = np.zeros(h * w, dtype=bool)
    for start in np.flatnonzero(flat):
        if seen[start]:
            continue
        seen[start] = True
        blob, stack = [start], [start]
        while stack:
            p = stack.pop()
            y, x = divmod(int(p), w)
            for dy in (-1, 0, 1):
                ny = y + dy
                if ny < 0 or ny >= h:
                    continue
                row = ny * w
                for dx in (-1, 0, 1):
                    nx = x + dx
                    if 0 <= nx < w:
                        q = row + nx
                        if flat[q] and not seen[q]:
                            seen[q] = True
                            stack.append(q)
                            blob.append(q)
        if len(blob) < min_size or (seed_flat is not None and not seed_flat[blob].any()):
            flat[blob] = False
    return flat.reshape(h, w)


# ---------------------------------------------------------------- styles

def line(gray: Image.Image, detail: float, weight: int) -> Image.Image:
    """Ink-pen outlines."""
    smooth = _prepare(gray)
    ink = _strokes(smooth, _lerp(2.0, 0.9, detail), _lerp(0.07, 0.16, detail), int(_lerp(30, 8, detail)))
    return _to_1bit(_thicken(ink, weight))


def coloring(gray: Image.Image, detail: float, weight: int) -> Image.Image:
    """Bold, clean outlines and no shading: something to color in."""
    smooth = _prepare(gray, 5).filter(ImageFilter.MedianFilter(5))
    ink = _strokes(smooth, _lerp(2.4, 1.3, detail), _lerp(0.08, 0.14, detail), int(_lerp(80, 30, detail)))
    return _to_1bit(_thicken(ink, weight + 1))


def sketch(gray: Image.Image, detail: float, weight: int) -> Image.Image:
    """Soft pencil: the 'color dodge' trick, then fine grain like graphite on paper."""
    gray = _prepare(gray, 1)
    g = _f(gray)
    inv_blur = 1.0 - _blur(gray, _lerp(10.0, 3.0, detail))
    dodge = np.clip(g / np.maximum(1.0 - inv_blur, 1e-3), 0.0, 1.0)
    tone = dodge ** _lerp(1.6, 2.6, (weight - 1) / 2)            # darker strokes for heavier weight
    tone = 0.85 * tone + 0.15 * np.clip(g * 1.3, 0, 1)         # keep a little of the shading
    img = Image.fromarray((tone * 255).astype(np.uint8), "L")
    return img.convert("1", dither=FLOYD)


def poster(gray: Image.Image, detail: float, weight: int) -> Image.Image:
    """Screen-print look: black, white, and one mid tone drawn as diagonal stripes."""
    smooth = _prepare(gray, 5 if detail < 0.5 else 3)
    g = _blur(smooth, _lerp(2.0, 0.6, detail))
    lo, hi = np.quantile(g, [0.25, 0.55])
    h, w = g.shape
    yy, xx = np.mgrid[0:h, 0:w]
    stripes = ((xx + yy) % (3 + weight)) == 0
    ink = (g < lo) | ((g < hi) & stripes)
    ink |= _remove_specks(_dog_lines(smooth, 1.4, 0.05), 20)  # outlines keep faces in the light areas
    return _to_1bit(ink)


def halftone(gray: Image.Image, detail: float, weight: int) -> Image.Image:
    """Newspaper dots on a 45 degree grid; bigger dots where it's darker."""
    cell = _lerp(9.0, 4.5, detail)
    g = _blur(_prepare(gray, 1), cell / 3)
    h, w = g.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    u = (xx + yy) / np.sqrt(2.0) / cell
    v = (xx - yy) / np.sqrt(2.0) / cell
    du = u - np.round(u)
    dv = v - np.round(v)
    dist = np.sqrt(du * du + dv * dv)                          # 0 at a dot center, ~0.7 at the corners
    dark = np.clip(1.0 - g, 0, 1) ** 1.25                      # a touch lighter: thermal dots spread
    radius = np.sqrt(dark / np.pi) * 1.2                       # dot area ~ darkness, merging near black
    return _to_1bit(dist < radius)


def hatch(gray: Image.Image, detail: float, weight: int) -> Image.Image:
    """Pen hatching: more crossing stroke layers where it's darker, plus thin outlines."""
    smooth = _prepare(gray)
    g = _blur(smooth, 1.2)
    h, w = g.shape
    yy, xx = np.mgrid[0:h, 0:w]
    s = int(round(_lerp(7, 4, detail)))
    layers = [
        (0.70, (xx + yy) % s == 0),
        (0.50, (xx - yy) % s == 0),
        (0.32, yy % s == 0),
        (0.16, (xx + yy + s // 2) % s == 0),
    ]
    ink = np.zeros_like(g, dtype=bool)
    for level, pattern in layers:
        ink |= (g < level) & pattern
    outline = _dog_lines(smooth, _lerp(2.0, 1.0, detail), 0.06)
    ink |= _thicken(_remove_specks(outline, 12), max(1, weight - 1))
    return _to_1bit(ink)
