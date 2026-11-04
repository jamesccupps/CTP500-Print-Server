"""AI line art: the Informative Drawings models (Chan, Durand, Isola, CVPR 2022; MIT license), as ONNX
conversions, run with onnxruntime. They draw people and things the way an artist would, where edge detection
breaks into dashes on ordinary (dim, soft, busy) photos.

    lineart     clean line art        huggingface.co/rocca/informative-drawings-line-art-onnx
    opensketch  looser artist sketch  huggingface.co/backnotprop/informative-drawings-image-to-opensketch-onnx

Input: RGB 0..1, [1, 3, H, W], any size. Output: [1, 1, H, W], 1 = paper. ~0.5 s at 512 px on a desktop CPU.
The models live in <project>/models (or $CTP500_MODELS); pi/install.sh downloads them. Without onnxruntime or the
files, available() is False and the styles fall back to edge detection.
"""

from __future__ import annotations

import collections
import os
import threading

import numpy as np
from PIL import Image, ImageFilter, ImageOps

MODELS = {
    "lineart": ("https://huggingface.co/rocca/informative-drawings-line-art-onnx/resolve/main/model.onnx",
                "1fef40b8f7126d827e30fbebccf95ae9b0b391795df926bf9366a821bad4f498"),
    "opensketch": ("https://huggingface.co/backnotprop/informative-drawings-image-to-opensketch-onnx/resolve/main/model.onnx",
                   "4ed7a5c28aadf8655fbbafe0ed5340be2beab9ad1baa84894bbabeb5d5c9bb4b"),
    # face finder (faces.py): YuNet, MIT, OpenCV Zoo's dynamic-size export, pinned to the commit that added it
    "faces": ("https://media.githubusercontent.com/media/opencv/opencv_zoo/26cc381e4d2594bb9f47a26eb8fd96c94a13660d/"
              "models/face_detection_yunet/face_detection_yunet_2026may.onnx",
              "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0"),
    # depth (relief plaques, litho.relief): Depth Anything V2 Small, Apache-2.0, the Transformers.js ONNX export
    "depth": ("https://huggingface.co/onnx-community/depth-anything-v2-small/resolve/main/onnx/model.onnx",
              "afb6a5c28f3b6bf1618c6e43f02073ef9dfdc70e937502d51603e57b0a1df10c"),
}

_sessions: dict = {}
_lock = threading.Lock()
PROVIDERS = ["CPUExecutionProvider"]   # the PC helper puts its graphics card (CUDA) first

# The model's cost grows with its pixel count; past this width a 48 mm print gains nothing. Sideways prints asked
# for ~830 px and took ~5 s per drawing on the Pi (2026-10-30); 640 cuts that by ~40%.
MAX_MODEL_WIDTH = 640
# Recent drawings, so flipping between styles or settings on one photo doesn't redraw it every time.
_drawings: collections.OrderedDict = collections.OrderedDict()
_DRAWINGS_KEPT = 8
# Set by the print server: (model name, the model's input picture) -> the drawing from the desktop PC's graphics
# card, or None to draw it here (2026-11-02; the PC draws in ~0.1 s, the Pi in ~2.5 s).
REMOTE = None


def models_dir() -> str:
    return os.environ.get("CTP500_MODELS") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")


def model_path(name: str) -> str:
    return os.path.join(models_dir(), f"{name}.onnx")


def download(name: str) -> str:
    """Fetch a model into models_dir() unless it's already there; refuses a file whose SHA-256 doesn't match."""
    import hashlib
    import urllib.request

    url, sha256 = MODELS[name]
    path = model_path(name)
    if os.path.exists(path):
        with open(path, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() == sha256:
                return "already there"
    os.makedirs(models_dir(), exist_ok=True)
    tmp = path + ".part"
    with urllib.request.urlopen(url, timeout=120) as res, open(tmp, "wb") as out:
        while True:
            chunk = res.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    with open(tmp, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    if digest != sha256:
        os.remove(tmp)
        raise RuntimeError(f"{name}: downloaded file has SHA-256 {digest}, expected {sha256}; not installed")
    os.replace(tmp, path)
    return "downloaded"


def available(name: str = "lineart") -> bool:
    if not os.path.exists(model_path(name)):
        return False
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _session(name: str):
    with _lock:
        if name not in _sessions:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            _sessions[name] = ort.InferenceSession(model_path(name), opts, providers=PROVIDERS)
        return _sessions[name]


def local_contrast(rgb: Image.Image, strength: float = 0.7) -> Image.Image:
    """Dehaze: stretch brightness within each neighbourhood ((L - local mean) / local spread, mixed back in),
    keeping the color. The MEMENTO camera's indoor shots have a bright haze over the middle (lens smudge or case
    window, plus pink LED room light) that washed faces out to blank ovals; this brings eyes, brows and mouths
    back.
    Tuned 2026-10-30 on its shots: neighbourhood sigma ~2.3% of the width, strength 0.7."""
    work = rgb.convert("RGB")
    if work.width > 1024:
        work = work.resize((1024, max(1, round(work.height * 1024 / work.width))), Image.LANCZOS)
    y, cb, cr = work.convert("YCbCr").split()
    sigma = max(2.0, work.width * 0.0234)
    lum = np.asarray(y, dtype=np.float32) / 255.0

    def blur(a: np.ndarray) -> np.ndarray:
        img = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8))
        return np.asarray(img.filter(ImageFilter.GaussianBlur(sigma)), dtype=np.float32) / 255.0

    mean = blur(lum)
    spread = np.sqrt(np.clip(blur(lum * lum) - mean * mean, 0, None)) + 0.04
    normalized = np.clip(0.5 + (lum - mean) / spread * 0.18, 0, 1)
    out = (1 - strength) * lum + strength * normalized
    y2 = Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8))
    return Image.merge("YCbCr", (y2, cb, cr)).convert("RGB")


def haze(rgb: Image.Image) -> float:
    """Local contrast in the middle of the picture (std of brightness within ~2.3%-of-width neighbourhoods).
    Measured 2026-10-30: hazy MEMENTO selfie 0.026, dim indoor shots 0.046-0.071, a well-lit shot 0.095,
    studio photos 0.15-0.17."""
    small = rgb.convert("L")
    small = small.resize((512, max(1, round(small.height * 512 / small.width))), Image.BILINEAR)
    a = np.asarray(small, dtype=np.float32) / 255.0

    def blur(x: np.ndarray) -> np.ndarray:
        return np.asarray(Image.fromarray((np.clip(x, 0, 1) * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(12)), dtype=np.float32) / 255.0

    mean = blur(a)
    spread = np.sqrt(np.clip(blur(a * a) - mean * mean, 0, None))
    h, w = a.shape
    return float(spread[h // 4:3 * h // 4, w // 4:3 * w // 4].mean())


def dehaze_strength(rgb: Image.Image) -> float:
    """How hard to dehaze: 0.7 for hazy shots (their faces vanish otherwise), easing to 0.2 for clear ones
    (a strong boost there draws skin texture and sensor grain as harsh marks)."""
    c = haze(rgb)
    return 0.2 + 0.5 * min(1.0, max(0.0, (0.10 - c) / 0.07))


def draw(img: Image.Image, name: str = "lineart", width: int = 512, dehaze: bool = True) -> Image.Image:
    """The model's drawing of a picture, as grayscale (255 = paper), `width` pixels wide. The picture is lightly
    smoothed (sensor grain otherwise turns into scratchy marks on faces), dehazed as much as it needs, and its
    contrast stretched first. Small faces are drawn again from a close crop (_redraw_faces)."""
    rgb = img.convert("RGB")
    width = max(64, min(int(width), MAX_MODEL_WIDTH))
    key = (name, width, dehaze, FACE_DETAIL, rgb.size, rgb.resize((24, 18), Image.BILINEAR).tobytes())
    with _lock:
        if key in _drawings:
            _drawings.move_to_end(key)
            return _drawings[key].copy()
    drawing = _run(name, rgb, width, dehaze)
    if FACE_DETAIL:
        drawing = _redraw_faces(drawing, rgb, name, dehaze)
    with _lock:
        _drawings[key] = drawing
        while len(_drawings) > _DRAWINGS_KEPT:
            _drawings.popitem(last=False)
    return drawing.copy()


def _run(name: str, rgb: Image.Image, width: int, dehaze: bool) -> Image.Image:
    if rgb.width > 2 * width:  # the blur and dehaze below don't need more than ~2x the model's width
        rgb = rgb.resize((2 * width, max(1, round(rgb.height * 2 * width / rgb.width))), Image.LANCZOS)
    rgb = rgb.filter(ImageFilter.GaussianBlur(max(0.3, rgb.width / 1024)))
    if dehaze:
        rgb = local_contrast(rgb, dehaze_strength(rgb))
    rgb = ImageOps.autocontrast(rgb, cutoff=1)
    height = max(64, round(rgb.height * width / rgb.width))
    picture = rgb.resize((width, height), Image.LANCZOS)
    if REMOTE is not None:
        drawing = REMOTE(name, picture)
        if drawing is not None and drawing.size == picture.size:
            return drawing
    return infer(name, picture)


def infer(name: str, picture: Image.Image) -> Image.Image:
    """One run of the model on an RGB picture already sized for it (the PC helper runs this same step)."""
    x = np.asarray(picture.convert("RGB"), dtype=np.float32) / 255.0
    x = np.ascontiguousarray(x.transpose(2, 0, 1)[None])
    session = _session(name)
    with _lock:  # one inference at a time: the Pi has 4 cores and they're all used per run
        y = session.run(None, {session.get_inputs()[0].name: x})[0][0, 0]
    return Image.fromarray((np.clip(y, 0.0, 1.0) * 255).astype(np.uint8), "L")


def depth(img: Image.Image, long_side: int = 518) -> np.ndarray:
    """How near each point of the picture is (Depth Anything V2 Small): a float array, larger = nearer (relative,
    not metres), at the model's working size (long side 518, both sides multiples of 14)."""
    rgb = img.convert("RGB")
    scale = long_side / max(rgb.size)
    w, h = (max(14, round(v * scale / 14) * 14) for v in rgb.size)
    x = np.asarray(rgb.resize((w, h), Image.BICUBIC), np.float32) / 255.0
    x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    x = np.ascontiguousarray(x.transpose(2, 0, 1)[None])
    session = _session("depth")
    with _lock:
        y = session.run(None, {session.get_inputs()[0].name: x})[0]
    return np.asarray(y, np.float32).reshape(h, w)


# A face is often ~40-150 of the drawing's 640 px (a group shot from across the room), too few for eyes and a mouth;
# drawn again from a crop around it at FACE_WIDTH, its own contrast stretched, it gets them (2026-10-31).
FACE_DETAIL = True
FACE_WIDTH = 224        # px the face is drawn at
FACES_REDRAWN = 2       # the biggest few (each is one more model run, ~1 s on the Pi)
FACE_GROW = 1.8         # crop around the face: hair, chin, room to blend
SMALL_FACE = 112        # px in the drawing (~65 dots on paper): bigger faces came out scratchier redrawn


def _redraw_faces(drawing: Image.Image, rgb: Image.Image, name: str, dehaze: bool) -> Image.Image:
    from . import faces
    s = drawing.width / rgb.width
    small = [f for f in faces.find(rgb) if f.w * s < SMALL_FACE]
    if not small:
        return drawing
    out = np.asarray(drawing, dtype=np.float32).copy()
    for f in sorted(small, key=lambda f: -f.w)[:FACES_REDRAWN]:
        l, t, r, b = f.box(FACE_GROW)
        l, t, r, b = max(0, round(l)), max(0, round(t)), min(rgb.width, round(r)), min(rgb.height, round(b))
        x0, y0 = round(l * s), round(t * s)
        rw, rh = min(out.shape[1], round(r * s)) - x0, min(out.shape[0], round(b * s)) - y0
        if rw < 8 or rh < 8:
            continue
        crop = rgb.crop((l, t, r, b))
        face = _run(name, crop, min(512, round((r - l) * FACE_WIDTH / f.w)), dehaze)
        face = np.asarray(face.resize((rw, rh), Image.LANCZOS), dtype=np.float32)
        # the close drawing inside an ellipse around the face, fading out to the whole-picture drawing
        cx, cy = (f.x + f.w / 2) * s - x0, (f.y + f.h / 2) * s - y0
        yy, xx = np.mgrid[0:rh, 0:rw].astype(np.float32)
        k = 0.4 * FACE_GROW * s                         # mask = 0 at the crop's edge (d = 1.25)
        d = np.sqrt(((xx - cx) / (f.w * k)) ** 2 + ((yy - cy) / (f.h * k)) ** 2)
        mask = np.clip((1.25 - d) / 0.5, 0, 1)
        region = out[y0:y0 + rh, x0:x0 + rw]
        out[y0:y0 + rh, x0:x0 + rw] = mask * face + (1 - mask) * region
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "L")
