"""Lithophanes: a photo as a thin panel for the 3D printer (made for a Bambu Lab A1 mini) that shows the picture
when light shines through it. Thick = dark, thin = light.

The panel is a height map: one vertex every RES_MM, thickness from MIN_MM (brightest) to MAX_MM (darkest), linear
in the picture's sRGB value. PLA lets light through roughly as exp(-k * thickness), which makes a linear thickness
ramp come out close to the photo's own gamma, so no extra curve is needed. The thickness is then rounded to whole
print layers and cleared of specks (terrace), which made the prints ~2x quicker. A frame of FRAME_MM at full
thickness runs round the edge: it stiffens the panel and keeps the side walls straight (the mesh stays watertight).

Print times are Bambu Studio's estimates for the A1 mini at 0.24mm Draft with sparse infill 100% and 1 wall loop
(2026-11-01, measured with its command line on a 4:3 photo): flat 60 / 80 / 110 mm = 12 / 20 / 35 min, curved
23 / 33 / 49 min of printing, plus ~6 min for the printer's own start (80 mm flat: 26 min in all). The first
design (0.8-3.0 mm, 0.2 mm vertices, Standard profile) printed in 43 min at 80 mm.

Shapes: "flat" lies on the bed with the relief up; "curved" is the same panel wrapped round a CURVE_DEGREES arc, standing
up with the relief outside, so it stands by itself with a light (an LED tea light, never a candle) inside. Either way
the relief faces the viewer and the picture reads the right way round. Files are binary STL in millimetres.
"""

from __future__ import annotations

import hashlib
import io
import struct
import threading
from collections import OrderedDict

import numpy as np
from PIL import Image, ImageFilter, ImageOps

SIZES = (60, 80, 110)           # long side in mm; the A1 mini's bed is 180 x 180 x 180
DEFAULT_SIZE = 80
SHAPES = ("flat", "curved")
# Made for quick prints first (2026-11-01): thin, and terraced to the
# A1 mini's 0.24 mm Draft layers (first layer 0.2), so every thickness is a whole number of layers.
FIRST_LAYER_MM = 0.2
LAYER_MM = 0.24
MIN_LAYERS, MAX_LAYERS = 3, 9   # 0.68 mm (brightest) to 2.12 mm (darkest)
MIN_MM = FIRST_LAYER_MM + (MIN_LAYERS - 1) * LAYER_MM
MAX_MM = FIRST_LAYER_MM + (MAX_LAYERS - 1) * LAYER_MM
SPECKS = 3                      # odd: specks of a layer smaller than this many samples across are dropped
FRAME_MM = 3.0                  # frame width, at MAX_MM
CURVE_DEGREES = 120.0
RES_MM = 0.4                    # one vertex per nozzle width: finer just adds travel and file size
PREVIEW_K = 1.3                 # light falloff per mm in white PLA, for the preview only (roughly 1:17 over 2.2 mm)
LIGHT = (255, 238, 210)         # warm white, like an LED tea light


def _picture(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.draft("RGB", (2048, 2048))          # phone photos are 12 MP; the panel needs ~500 px
    return ImageOps.exif_transpose(img).convert("RGB")


def _tones(rgb: Image.Image, w: int, h: int) -> np.ndarray:
    """The picture as brightness 0..1 on a w x h grid, prepared the way the thermal prints are: dehazed as much as it
    needs, stretched, faces brought to a good brightness, lightly sharpened."""
    from . import ai, styles
    work = ai.local_contrast(rgb, min(0.55, ai.dehaze_strength(rgb)))
    gray = ImageOps.autocontrast(work.convert("L"), cutoff=1)
    if styles.FACE_TONES:
        gray = styles._face_tones(gray, rgb)
    gray = gray.resize((w, h), Image.LANCZOS).filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
    return np.asarray(gray, dtype=np.float32) / 255.0


_cache: OrderedDict = OrderedDict()
_lock = threading.Lock()


def heights(data: bytes, size_mm: float = DEFAULT_SIZE) -> tuple[np.ndarray, float]:
    """(thickness in mm per vertex, rows x columns; the vertex spacing in mm). The longer side of the photo is
    size_mm. Remembered for the last few photos, so the preview and the download don't both do the work."""
    size_mm = float(size_mm)
    key = (hashlib.sha1(data).hexdigest(), size_mm)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    rgb = _picture(data)
    res = RES_MM
    scale = size_mm / max(rgb.size)
    w = round(rgb.width * scale / res) + 1
    h = round(rgb.height * scale / res) + 1
    t = terrace(MAX_MM - (MAX_MM - MIN_MM) * _tones(rgb, w, h))
    edge = max(1, round(FRAME_MM / res))
    t[:edge, :] = t[-edge:, :] = t[:, :edge] = t[:, -edge:] = MAX_MM
    out = (t, res)
    with _lock:
        _cache[key] = out
        while len(_cache) > 4:
            _cache.popitem(last=False)
    return out


def terrace(t: np.ndarray) -> np.ndarray:
    """Thickness rounded to whole layers, then specks dropped: a lone sample one layer up is a tiny island the
    printer has to travel to and wall in, every layer (half the print time on a raw photo). A mode filter keeps
    edges, unlike a blur, which smeared the faces."""
    layers = np.clip(np.rint((t - FIRST_LAYER_MM) / LAYER_MM) + 1, MIN_LAYERS, MAX_LAYERS).astype(np.uint8)
    if SPECKS > 1:
        layers = np.asarray(Image.fromarray(layers).filter(ImageFilter.ModeFilter(SPECKS)))
    return (FIRST_LAYER_MM + (layers.astype(np.float32) - 1) * LAYER_MM).astype(np.float32)


def dimensions(t: np.ndarray, res: float) -> tuple[float, float]:
    """(width, height) of the panel in mm, before any curving."""
    return (t.shape[1] - 1) * res, (t.shape[0] - 1) * res


def preview(t: np.ndarray, width: int = 480) -> bytes:
    """PNG of the panel lit from behind: light falls off exponentially with thickness (linear light, so it's
    encoded for the screen like any photo)."""
    glow = np.exp(-PREVIEW_K * (t - MIN_MM)) ** (1 / 2.2)
    rgb = (glow[..., None] * np.array(LIGHT, dtype=np.float32)).clip(0, 255).astype(np.uint8)
    img = Image.fromarray(rgb, "RGB")
    img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# Relief plaques (2026-11-02): the photo as a carving, nearer things standing out, from a depth model
# (ai.depth, Depth Anything V2 Small). Opaque, any filament color, lit from the side. The room's broad slope (floor
# near, back wall far) is mostly taken out, so the people stand out rather than the floor.
RELIEF_BASE_MM = 1.6        # the plate everything stands on
RELIEF_MM = 4.0             # how far the nearest things stand out
RELIEF_DETAIL_MM = 0.5      # fine texture from the photo's light and dark (eyes, hair, folds)
RELIEF_FLATTEN = 0.65       # how much of the broad depth slope is taken out
RELIEF_RIM_MM = 1.0         # the frame stands this far above the plate


def _blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur of a float array (separable; edges repeated)."""
    if sigma <= 0.3:
        return a
    r = max(1, int(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    out = np.pad(a, ((0, 0), (r, r)), mode="edge")
    out = np.apply_along_axis(lambda v: np.convolve(v, k, mode="valid"), 1, out)
    out = np.pad(out, ((r, r), (0, 0)), mode="edge")
    return np.apply_along_axis(lambda v: np.convolve(v, k, mode="valid"), 0, out)


def relief_heights(data: bytes, size_mm: float = DEFAULT_SIZE) -> tuple[np.ndarray, float]:
    """(height in mm per vertex, the vertex spacing) of a relief plaque of the photo; long side size_mm."""
    from . import ai
    size_mm = float(size_mm)
    key = ("relief", hashlib.sha1(data).hexdigest(), size_mm)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    rgb = _picture(data)
    res = RES_MM
    scale = size_mm / max(rgb.size)
    w, h = round(rgb.width * scale / res) + 1, round(rgb.height * scale / res) + 1
    d = Image.fromarray(ai.depth(rgb), "F").resize((w, h), Image.BICUBIC)
    d = np.asarray(d, np.float32)
    d = d - RELIEF_FLATTEN * _blur(d, 0.12 * w)
    lo, hi = np.percentile(d, [2, 99])
    d = np.clip((d - lo) / max(1e-6, hi - lo), 0, 1)
    gray = _tones(rgb, w, h)
    detail = np.clip((gray - _blur(gray, 2.0)) * 4, -1, 1)
    t = RELIEF_BASE_MM + RELIEF_MM * d + RELIEF_DETAIL_MM * detail
    t = np.maximum(t, RELIEF_BASE_MM * 0.6).astype(np.float32)
    edge = max(1, round(FRAME_MM / res))
    rim = RELIEF_BASE_MM + RELIEF_RIM_MM
    t[:edge, :] = t[-edge:, :] = t[:, :edge] = t[:, -edge:] = rim
    out = (t, res)
    with _lock:
        _cache[key] = out
        while len(_cache) > 4:
            _cache.popitem(last=False)
    return out


def relief_preview(t: np.ndarray, res: float, width: int = 480) -> bytes:
    """PNG of the plaque lit from the upper left, like a lamp beside it (light plaster color)."""
    gy, gx = np.gradient(t.astype(np.float64), res)
    n = np.stack([-gx, gy, np.ones_like(gx)], axis=-1)       # rows run down the picture: y up is -row
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    light = np.array([-0.55, 0.55, 0.63])
    light /= np.linalg.norm(light)
    shade = 0.28 + 0.72 * np.clip(n @ light, 0, 1)
    rgb = (shade[..., None] * np.array([236, 230, 220], np.float32)).clip(0, 255).astype(np.uint8)
    img = Image.fromarray(rgb, "RGB")
    img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def mesh(t: np.ndarray, res: float, shape: str = "flat") -> tuple[np.ndarray, np.ndarray]:
    """(vertices N x 3 in mm, triangles M x 3 indices), closed and outward-facing (counter-clockwise from outside).

    Built flat, with x along the columns, y up the picture and z the thickness: the relief on top, a flat back
    made of strips from the top edge to the bottom edge, and side walls (the frame keeps their outer edge straight,
    so the end walls can be fans). "curved" then wraps x round an arc: radius R + z, height y."""
    if shape not in SHAPES:
        raise ValueError(f"shape must be one of {', '.join(SHAPES)}")
    h, w = t.shape
    xs = np.arange(w, dtype=np.float64) * res
    ys = (h - 1 - np.arange(h, dtype=np.float64)) * res          # row 0 is the top of the picture
    gx, gy = np.meshgrid(xs, ys)
    front = np.stack([gx, gy, t.astype(np.float64)], axis=-1).reshape(-1, 3)
    top = np.stack([xs, np.full(w, ys[0]), np.zeros(w)], axis=-1)     # the back's top and bottom edges
    bottom = np.stack([xs, np.full(w, ys[-1]), np.zeros(w)], axis=-1)
    verts = np.concatenate([front, top, bottom])
    F = np.arange(h * w).reshape(h, w)
    T = h * w + np.arange(w)
    B = h * w + w + np.arange(w)

    a, b = F[:-1, :-1].ravel(), F[:-1, 1:].ravel()             # cell corners: a b
    c, d = F[1:, :-1].ravel(), F[1:, 1:].ravel()               #               c d
    tris = [np.stack([c, d, b], 1), np.stack([c, b, a], 1)]   # relief, facing +z
    j0, j1 = np.arange(w - 1), np.arange(1, w)
    tris += [np.stack([B[j0], T[j0], T[j1]], 1), np.stack([B[j0], T[j1], B[j1]], 1)]                # back, -z
    tris += [np.stack([T[j1], T[j0], F[0, j0]], 1), np.stack([T[j1], F[0, j0], F[0, j1]], 1)]      # top, +y
    tris += [np.stack([B[j0], B[j1], F[-1, j1]], 1), np.stack([B[j0], F[-1, j1], F[-1, j0]], 1)]   # bottom, -y
    i0, i1 = np.arange(h - 1), np.arange(1, h)
    tris += [np.stack([np.full(h - 1, B[0]), F[i1, 0], F[i0, 0]], 1), np.array([[B[0], F[0, 0], T[0]]])]  # left
    tris += [np.stack([np.full(h - 1, T[-1]), F[i0, -1], F[i1, -1]], 1), np.array([[T[-1], F[-1, -1], B[-1]]])]  # right
    tris = np.concatenate(tris).astype(np.int64)

    if shape == "curved":
        width = xs[-1]
        angle = np.radians(CURVE_DEGREES)
        radius = width / angle
        # picture's left edge on the viewer's left: the viewer looks from +y in the curved frame
        phi = np.pi / 2 - angle / 2 + verts[:, 0] / width * angle
        r = radius + verts[:, 2]
        verts = np.stack([r * np.cos(phi), r * np.sin(phi), verts[:, 1]], axis=-1)
        verts[:, 1] -= verts[:, 1].min()
        verts[:, 0] -= verts[:, 0].min()
    return verts, tris


def stl(verts: np.ndarray, tris: np.ndarray, name: str = "lithophane") -> bytes:
    """Binary STL (millimetres)."""
    v = verts[tris].astype(np.float32)                           # M x 3 x 3
    n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    rec = np.zeros(len(tris), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
    rec["n"], rec["v"] = n, v
    header = f"ctp500 lithophane {name}"[:80].encode("ascii", "replace").ljust(80, b" ")
    return header + struct.pack("<I", len(tris)) + rec.tobytes()


def make(data: bytes, size_mm: float = DEFAULT_SIZE, shape: str = "flat", name: str = "lithophane") -> bytes:
    """A photo straight to an STL."""
    t, res = heights(data, size_mm)
    return stl(*mesh(t, res, shape), name=name)
