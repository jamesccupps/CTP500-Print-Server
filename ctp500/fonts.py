"""Finding a usable TrueType font on Windows or Raspberry Pi OS."""

from __future__ import annotations

import os
from functools import lru_cache

from PIL import ImageFont

_WIN = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
_LINUX = "/usr/share/fonts/truetype"

# family -> (regular candidates, bold candidates); first file that exists wins
CANDIDATES = {
    "sans": (
        [f"{_LINUX}/dejavu/DejaVuSans.ttf", f"{_LINUX}/liberation/LiberationSans-Regular.ttf",
         f"{_LINUX}/noto/NotoSans-Regular.ttf", f"{_WIN}/segoeui.ttf", f"{_WIN}/arial.ttf"],
        [f"{_LINUX}/dejavu/DejaVuSans-Bold.ttf", f"{_LINUX}/liberation/LiberationSans-Bold.ttf",
         f"{_LINUX}/noto/NotoSans-Bold.ttf", f"{_WIN}/segoeuib.ttf", f"{_WIN}/arialbd.ttf"],
    ),
    "serif": (
        [f"{_LINUX}/dejavu/DejaVuSerif.ttf", f"{_LINUX}/liberation/LiberationSerif-Regular.ttf",
         f"{_LINUX}/noto/NotoSerif-Regular.ttf", f"{_WIN}/georgia.ttf", f"{_WIN}/times.ttf"],
        [f"{_LINUX}/dejavu/DejaVuSerif-Bold.ttf", f"{_LINUX}/liberation/LiberationSerif-Bold.ttf",
         f"{_LINUX}/noto/NotoSerif-Bold.ttf", f"{_WIN}/georgiab.ttf", f"{_WIN}/timesbd.ttf"],
    ),
    "mono": (
        [f"{_LINUX}/dejavu/DejaVuSansMono.ttf", f"{_LINUX}/liberation/LiberationMono-Regular.ttf",
         f"{_LINUX}/noto/NotoSansMono-Regular.ttf", f"{_WIN}/consola.ttf", f"{_WIN}/cour.ttf"],
        [f"{_LINUX}/dejavu/DejaVuSansMono-Bold.ttf", f"{_LINUX}/liberation/LiberationMono-Bold.ttf",
         f"{_LINUX}/noto/NotoSansMono-Bold.ttf", f"{_WIN}/consolab.ttf", f"{_WIN}/courbd.ttf"],
    ),
}
FAMILIES = tuple(CANDIDATES)
MAX_SIZE = 600    # px: 400 on the CTP500 (render caps by paper), 600 on 300 dpi paper


@lru_cache(maxsize=None)
def font_path(family: str = "sans", bold: bool = False) -> str | None:
    """Path of the font file for a family, or None. A family can also be a .ttf/.otf path."""
    if family and os.path.splitext(family)[1].lower() in (".ttf", ".otf", ".ttc"):
        return family if os.path.exists(family) else None
    regular, heavy = CANDIDATES.get(family, CANDIDATES["sans"])
    for path in (heavy if bold else regular):
        if os.path.exists(path):
            return path
    return None


@lru_cache(maxsize=256)
def load(family: str = "sans", size: int = 24, bold: bool = False):
    """(font, fake_bold). fake_bold is True when no bold face was found and the caller should
    thicken strokes instead."""
    size = max(6, min(MAX_SIZE, int(size)))
    path = font_path(family, bold)
    if path:
        return ImageFont.truetype(path, size), False
    if bold:
        path = font_path(family, False)
        if path:
            return ImageFont.truetype(path, size), True
    try:
        return ImageFont.load_default(size), bold
    except TypeError:  # Pillow < 10.1 has a single fixed-size bitmap font
        return ImageFont.load_default(), bold
