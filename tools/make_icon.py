"""Draws tools/ctp500.ico (the desktop shortcut's icon) to match web/icon.svg."""

import os

from PIL import Image, ImageDraw

S = 256
k = S / 64  # icon.svg is drawn on a 64-unit grid


def box(x, y, w, h):
    return [x * k, y * k, (x + w) * k - 1, (y + h) * k - 1]


img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
green, light, white = (31, 111, 92, 255), (232, 243, 239, 255), (255, 255, 255, 255)
d.rounded_rectangle(box(4, 4, 56, 56), radius=14 * k, fill=green)
d.rounded_rectangle(box(14, 22, 36, 22), radius=5 * k, fill=light)
d.rounded_rectangle(box(20, 12, 24, 12), radius=2 * k, fill=white)
paper = [(20, 40), (44, 40), (44, 54), (41, 52), (38, 54), (35, 52), (32, 54), (29, 52), (26, 54), (23, 52), (20, 54)]
d.polygon([(x * k, y * k) for x, y in paper], fill=white)
d.rounded_rectangle(box(24, 44, 16, 2), radius=1 * k, fill=green)
d.rounded_rectangle(box(24, 48, 11, 2), radius=1 * k, fill=green)
d.ellipse(box(42, 27, 4, 4), fill=green)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ctp500.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out)
