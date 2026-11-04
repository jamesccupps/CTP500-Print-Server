"""Face restoration on the PC's graphics card: each face found by YuNet (ctp500.faces) is lined up with the FFHQ
512 x 512 template from its five landmarks, redrawn by a restoration model (models.py), and blended back with a
soft-edged mask. Small camera boards take small, soft, grainy faces indoors; these models redraw them sharp.
"""

from __future__ import annotations

import threading

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import models

# eyes, nose, mouth corners of a face in the 512 x 512 crop the models were trained on (FFHQ / facexlib)
TEMPLATE = np.array([[192.98138, 239.94708], [318.90277, 240.1936], [256.63416, 314.01935],
                     [201.26117, 371.41043], [313.08905, 371.15118]], np.float64)
SIZE = 512
MIN_FACE = 16          # px: smaller faces are left alone (there's nothing to line up)
MAX_FACES = 6

_sessions: dict = {}
_lock = threading.Lock()
PROVIDERS = ["CPUExecutionProvider"]
IDLE_S = 900              # a model unused this long is let go (~0.5 GB of memory each; reloading takes ~2 s)
_used = 0.0


def drop_idle() -> bool:
    """Let go of the face models if none was used for IDLE_S (the helper calls this every minute)."""
    import time
    with _lock:
        if _sessions and time.monotonic() - _used > IDLE_S:
            _sessions.clear()
            return True
    return False


def _session(name: str):
    import time
    global _used
    with _lock:
        _used = time.monotonic()
        if name not in _sessions:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            _sessions[name] = ort.InferenceSession(models.fetch(name), opts, providers=PROVIDERS)
        return _sessions[name]


def _similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """The 2 x 3 similarity transform (rotation, uniform scale, shift) taking src points onto dst, least squares
    (Umeyama)."""
    mu_s, mu_d = src.mean(0), dst.mean(0)
    s, d = src - mu_s, dst - mu_d
    cov = d.T @ s / len(src)
    u, sig, vt = np.linalg.svd(cov)
    sign = np.eye(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        sign[1, 1] = -1
    rot = u @ sign @ vt
    scale = np.trace(np.diag(sig) @ sign) / s.var(0).sum()
    m = np.zeros((2, 3))
    m[:, :2] = scale * rot
    m[:, 2] = mu_d - m[:, :2] @ mu_s
    return m


def _invert(m: np.ndarray) -> np.ndarray:
    a = np.vstack([m, [0, 0, 1]])
    return np.linalg.inv(a)[:2]


def _warp(img: Image.Image, m: np.ndarray, size: tuple[int, int]) -> Image.Image:
    """img mapped through m (img coords -> output coords) into an image of `size`. PIL wants output -> input."""
    inv = _invert(m)
    return img.transform(size, Image.AFFINE, tuple(inv.reshape(-1)), resample=Image.BICUBIC)


def _mask() -> Image.Image:
    """Where the restored face replaces the photo, in the crop: a box inset from the edges, feathered (like
    FaceFusion's box mask), so hair and background past the face keep the photo's own look."""
    m = Image.new("L", (SIZE, SIZE), 0)
    pad = int(SIZE * 0.08)
    ImageDraw.Draw(m).rectangle([pad, pad, SIZE - pad, SIZE - pad], fill=255)
    return m.filter(ImageFilter.GaussianBlur(SIZE * 0.05))


_MASK = None


def enhance_crop(crop: Image.Image, model: str, fidelity: float = 0.7) -> Image.Image:
    """One 512 x 512 RGB face crop through the model. fidelity is CodeFormer's weight (0 = prettiest, 1 = most like
    the input); the other models have none."""
    x = np.asarray(crop.convert("RGB"), np.float32) / 255.0
    x = ((x - 0.5) / 0.5).transpose(2, 0, 1)[None]
    session = _session(model)
    feed = {}
    for i in session.get_inputs():
        if i.name == "weight":
            feed[i.name] = np.array([fidelity], np.float64)
        else:
            feed[i.name] = np.ascontiguousarray(x, np.float32)
    with _lock:
        y = session.run(None, feed)[0][0]
    y = (np.clip(y, -1, 1) + 1) / 2 * 255
    return Image.fromarray(np.rint(y.transpose(1, 2, 0)).astype(np.uint8), "RGB")


def restore(img: Image.Image, model: str, blend: float = 1.0, fidelity: float = 0.7) -> tuple[Image.Image, int]:
    """(the photo with its faces restored, how many). blend mixes the restored face with the original (1 = all
    restored)."""
    from ctp500 import faces
    global _MASK
    if _MASK is None:
        _MASK = _mask()
    rgb = img.convert("RGB")
    out = rgb.copy()
    done = 0
    for f in sorted(faces.find(rgb), key=lambda f: -f.w)[:MAX_FACES]:
        if f.w < MIN_FACE or len(f.points) != 5:
            continue
        m = _similarity(np.array(f.points, np.float64), TEMPLATE)          # photo -> crop
        crop = _warp(rgb, m, (SIZE, SIZE))
        fixed = enhance_crop(crop, model, fidelity)
        if blend < 1:
            fixed = Image.blend(crop, fixed, blend)
        inv = _invert(m)                                                    # crop -> photo
        back = _warp(fixed, inv, rgb.size)
        mask = _warp(_MASK, inv, rgb.size)
        out = Image.composite(back, out, mask)
        done += 1
    return out, done
