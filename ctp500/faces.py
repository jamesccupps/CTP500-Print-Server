"""Faces in photos: YuNet (Shiqi Yu et al., MIT license), the dynamic-size ONNX from OpenCV Zoo, run with
onnxruntime like the drawing models (ai.MODELS["faces"], ~230 KB). It finds faces from ~10 to ~300 px, so the
photo is searched at FIND_WIDTH. Decoding follows OpenCV's FaceDetectorYN (objdetect/src/face_detect.cpp): per
stride 8/16/32 a grid of cells, score = sqrt(cls * obj), box centre = (cell + offset) * stride, size = exp() * stride.

Used to draw faces at more detail (styles.apply_ai / thermal_photo outlines), to set a photo's tones for its faces
rather than the whole room, and to zoom a print in on the people.
"""

from __future__ import annotations

import collections
import threading
from dataclasses import dataclass

import numpy as np
from PIL import Image

FIND_WIDTH = 640       # camera shots are 1024 wide: faces from ~16 px to ~half the picture
MIN_SCORE = 0.8        # test shots: real faces 0.85-0.95, toys / patterns / blur <= 0.73 (55 photos, 2026-10-31)
NMS_IOU = 0.3

_found: collections.OrderedDict = collections.OrderedDict()
_lock = threading.Lock()


@dataclass(frozen=True)
class Face:
    x: float           # box, in the picture's own pixels
    y: float
    w: float
    h: float
    score: float
    eyes: tuple        # ((x, y), (x, y)): the two eyes
    points: tuple = () # all five landmarks (eye, eye, nose, mouth corner, mouth corner; image left first in
                       # each pair), for lining a face up with face restoration models (the PC helper)

    def box(self, grow: float = 1.0) -> tuple[float, float, float, float]:
        """(left, top, right, bottom), grown around the centre by `grow`."""
        cx, cy = self.x + self.w / 2, self.y + self.h / 2
        hw, hh = self.w * grow / 2, self.h * grow / 2
        return cx - hw, cy - hh, cx + hw, cy + hh


def available() -> bool:
    from . import ai
    return ai.available("faces")


def find(img: Image.Image) -> list[Face]:
    """The faces in a picture, best first; [] without the model. Remembered per picture (a print asks several
    times: tones, outlines, zoom)."""
    if not available():
        return []
    rgb = img.convert("RGB")
    key = (rgb.size, rgb.resize((24, 18), Image.BILINEAR).tobytes())
    with _lock:
        if key in _found:
            return _found[key]
    scale = min(1.0, FIND_WIDTH / rgb.width)
    small = rgb.resize((max(32, round(rgb.width * scale)), max(32, round(rgb.height * scale))), Image.BILINEAR)
    faces = [Face(f.x / scale, f.y / scale, f.w / scale, f.h / scale, f.score,
                  tuple((ex / scale, ey / scale) for ex, ey in f.eyes),
                  tuple((px / scale, py / scale) for px, py in f.points)) for f in _detect(small)]
    with _lock:
        _found[key] = faces
        while len(_found) > 32:
            _found.popitem(last=False)
    return faces


def _detect(rgb: Image.Image) -> list[Face]:
    from . import ai
    w, h = rgb.size
    pw, ph = -(-w // 32) * 32, -(-h // 32) * 32          # the network wants multiples of 32: pad right/bottom
    x = np.zeros((1, 3, ph, pw), np.float32)
    x[0, :, :h, :w] = np.asarray(rgb, np.float32)[:, :, ::-1].transpose(2, 0, 1)   # BGR, 0..255
    session = ai._session("faces")
    names = [o.name for o in session.get_outputs()]
    with ai._lock:
        out = dict(zip(names, session.run(None, {session.get_inputs()[0].name: x})))
    boxes, scores, eyes = [], [], []
    for stride in (8, 16, 32):
        cols, rows = pw // stride, ph // stride
        cls = np.clip(out[f"cls_{stride}"].reshape(-1), 0, 1)
        obj = np.clip(out[f"obj_{stride}"].reshape(-1), 0, 1)
        score = np.sqrt(cls * obj)
        keep = np.nonzero(score >= MIN_SCORE)[0]
        if not len(keep):
            continue
        bbox = out[f"bbox_{stride}"].reshape(-1, 4)[keep]
        kps = out[f"kps_{stride}"].reshape(-1, 10)[keep]
        c, r = keep % cols, keep // cols
        cx, cy = (c + bbox[:, 0]) * stride, (r + bbox[:, 1]) * stride
        bw, bh = np.exp(bbox[:, 2]) * stride, np.exp(bbox[:, 3]) * stride
        boxes.append(np.stack([cx - bw / 2, cy - bh / 2, bw, bh], 1))
        scores.append(score[keep])
        eyes.append(np.stack([(kps[:, 2 * k + axis] + (c if axis == 0 else r)) * stride
                              for k in range(5) for axis in (0, 1)], 1))     # all five landmarks
    if not boxes:
        return []
    boxes, scores, eyes = np.concatenate(boxes), np.concatenate(scores), np.concatenate(eyes)
    found = []
    for i in _nms(boxes, scores):
        bx, by, bw, bh = (float(v) for v in boxes[i])
        e = [float(v) for v in eyes[i]]
        found.append(Face(bx, by, bw, bh, float(scores[i]), ((e[0], e[1]), (e[2], e[3])),
                          tuple((e[2 * k], e[2 * k + 1]) for k in range(5))))
    return found


def _nms(boxes: np.ndarray, scores: np.ndarray) -> list[int]:
    order = list(np.argsort(-scores))
    x1, y1 = boxes[:, 0], boxes[:, 1]
    x2, y2 = x1 + boxes[:, 2], y1 + boxes[:, 3]
    area = boxes[:, 2] * boxes[:, 3]
    kept = []
    while order:
        i = order.pop(0)
        kept.append(i)
        rest = np.array(order, dtype=int)
        if not len(rest):
            break
        iw = np.clip(np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]), 0, None)
        ih = np.clip(np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]), 0, None)
        iou = iw * ih / (area[i] + area[rest] - iw * ih + 1e-6)
        order = [j for j, o in zip(order, iou) if o <= NMS_IOU]
    return kept


ZOOM_MAX = 2.0         # zoom in at most 2x: the crop keeps at least half the photo's width


def zoom_box(size: tuple[int, int], found: list[Face]) -> tuple[int, int, int, int] | None:
    """The part of a photo to print when zooming in on the people: heads and shoulders, at the photo's own shape,
    no closer than ZOOM_MAX. Faces under a third the size of the biggest (someone far behind) don't count. None
    when that would be (nearly) the whole photo."""
    width, height = size
    big = max((f.w for f in found), default=0)
    people = [f for f in found if f.w >= big / 3]
    if not people:
        return None
    left = min(f.x - 0.9 * f.w for f in people)
    right = max(f.x + 1.9 * f.w for f in people)
    top = min(f.y - 0.7 * f.h for f in people)
    bottom = max(f.y + 2.6 * f.h for f in people)
    aspect = width / height
    w = max(right - left, (bottom - top) * aspect, width / ZOOM_MAX)
    if w >= 0.85 * width:
        return None
    h = w / aspect
    x0 = min(max(0.0, (left + right - w) / 2), width - w)
    y0 = min(max(0.0, (top + bottom - h) / 2), height - h)
    return round(x0), round(y0), round(x0 + w), round(y0 + h)
