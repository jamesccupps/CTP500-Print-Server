"""Draws docs/images/prints.png: example prints, rendered dot for dot by the same code that prints them, on paper.

    python tools/make_examples.py <folder with astronaut.jpg, chelsea.jpg, coffee.jpg>

The photos are scikit-image's sample images (astronaut: NASA, public domain; chelsea and coffee: CC0), saved as JPEG
with `skimage.data.<name>()`. Needs the AI models (python -m ctp500 models) for the outlines and line drawing.
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw, ImageFilter

from ctp500 import camera, render

NOW = datetime.datetime(2026, 11, 4, 9, 30)
PAPER, INK = (250, 248, 243), (38, 38, 42)
NOTE = """# Saturday
## Shopping
- [ ] eggs
- [ ] coffee beans
- [x] bread
---
Milk ..... $3.49
Coffee ..... $8.99
> Back by noon!
[qr] https://example.com/list"""


def photo(folder: str, name: str, style: str, frame: bool, caption: str) -> dict:
    with open(os.path.join(folder, name + ".jpg"), "rb") as f:
        data = f.read()
    cfg = {"camera_style": style, "camera_frame": frame, "camera_caption": caption, "camera_sideways": False,
           "camera_detail": 0.5, "camera_weight": 2}
    return camera.print_doc(data, name + ".jpg", cfg)


def strip(doc: dict) -> Image.Image:
    doc = dict(doc, padding_top_mm=4, padding_bottom_mm=4)
    dots = render.render(doc, now=NOW).convert("L")
    paper = Image.new("RGB", dots.size, PAPER)
    paper.paste(Image.new("RGB", dots.size, INK), mask=dots.point(lambda v: 255 - v))
    return paper


def main(folder: str) -> None:
    columns = [[strip({"markup": NOTE})],
               [strip(photo(folder, "astronaut", "photoline", True, "{date}"))],
               [strip(photo(folder, "chelsea", "line", False, "")), strip(photo(folder, "coffee", "photo", False, ""))],
               [strip({"blocks": [{"type": "banner", "text": "Hi!"}]})]]
    gap, pad = 36, 40
    w = sum(c[0].width for c in columns) + gap * (len(columns) - 1) + 2 * pad
    h = max(sum(s.height for s in c) + gap * (len(c) - 1) for c in columns) + 2 * pad
    out = Image.new("RGB", (w, h), (225, 229, 226))
    places = []
    x = pad
    for c in columns:
        y = pad
        for s in c:
            places.append((s, x, y))
            y += s.height + gap
        x += c[0].width + gap
    shadow = Image.new("L", (w, h), 0)
    for s, x, y in places:
        ImageDraw.Draw(shadow).rectangle([x + 6, y + 8, x + s.width + 6, y + s.height + 8], fill=90)
    out.paste((150, 155, 152), mask=shadow.filter(ImageFilter.GaussianBlur(9)))
    for s, x, y in places:
        out.paste(s, (x, y))
    dest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "images", "prints.png")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    out.save(dest, optimize=True)
    print(dest, out.size)


if __name__ == "__main__":
    main(sys.argv[1])
