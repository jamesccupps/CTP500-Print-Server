"""Print a tone calibration strip: 11 even gray steps (0..100% ink, Floyd-Steinberg so each patch's dot coverage is
what it says) at each darkness, one job per darkness, back to back so they come out as one strip. Photograph it
(Photos page, Take photo) to measure how dark each coverage really prints (dot gain), for styles.thermal_photo.

    python tools/calibration_strip.py                 # prints it on the server's printer (~6 cm)
    python tools/calibration_strip.py --preview a.png # just the picture

The server is the command line's (python -m ctp500 config server=http://<pi>:8500), or CTP500_SERVER.
"""
import base64
import io
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw

from ctp500 import config, fonts, raster

CFG = config.load()
SERVER = (CFG.get("server") or "").rstrip("/")
STEPS = 11
PATCH_W, GAP, PATCH_H = 30, 4, 64


def strip(label: str) -> Image.Image:
    font, _ = fonts.load("sans", 20, True)
    img = Image.new("L", (384, 26 + PATCH_H + 8), 255)
    d = ImageDraw.Draw(img)
    d.text((4, 0), label, font=font, fill=0)
    d.text((276, 2), "0 -> 100% ink", font=fonts.load("sans", 14, False)[0], fill=0)
    top = 26
    x0 = (384 - (STEPS * PATCH_W + (STEPS - 1) * GAP)) // 2
    for i in range(STEPS):
        ink = i / (STEPS - 1)
        gray = Image.new("L", (PATCH_W, PATCH_H), round(255 * (1 - ink)))
        patch = raster.to_1bit(gray, "floyd").convert("L")
        img.paste(patch, (x0 + i * (PATCH_W + GAP), top))
    # a thin frame around the row so the patches are easy to find in a photo
    d.rectangle([x0 - 4, top - 4, x0 + STEPS * PATCH_W + (STEPS - 1) * GAP + 3, top + PATCH_H + 3], outline=0, width=2)
    return img.point(lambda v: 255 if v > 128 else 0).convert("1")


def send(img: Image.Image, darkness: str, last: bool) -> None:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc = {"blocks": [{"type": "image", "data": base64.b64encode(buf.getvalue()).decode(), "style": "threshold",
                       "name": f"Calibration {darkness}"}],
           "darkness": darkness, "padding_top_mm": 3 if darkness == "light" else 1,
           "padding_bottom_mm": 6 if last else 0, "feed": 2 if last else 0}
    headers = {"Content-Type": "application/json"}
    if CFG.get("token"):
        headers["Authorization"] = "Bearer " + CFG["token"]
    req = urllib.request.Request(SERVER + "/api/print?wait=1&timeout=60", json.dumps(doc).encode(), method="POST",
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=90) as res:
        print(darkness, res.status, json.loads(res.read())["job"]["status"])


if __name__ == "__main__":
    levels = ["light", "normal", "dark", "darkest"]
    if "--preview" in sys.argv:
        Image.fromarray  # noqa
        parts = [strip(l.upper()) for l in levels]
        out = Image.new("1", (384, sum(p.height for p in parts)), 1)
        y = 0
        for p in parts:
            out.paste(p, (0, y))
            y += p.height
        out.save(sys.argv[-1])
    else:
        if not SERVER:
            sys.exit("no server: python -m ctp500 config server=http://<pi>:8500 (or use --preview out.png)")
        for i, level in enumerate(levels):
            send(strip(level.upper()), level, i == len(levels) - 1)
