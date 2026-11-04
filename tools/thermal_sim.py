"""Simulated thermal print: each black dot prints as a disk a bit bigger than the dot pitch (dot gain), so dense
patterns merge. Gives a gray image of how the paper looks from reading distance, for comparing dithers.

2026-10-30: compared the photo style (Atkinson + midtone lift) with Floyd-Steinberg and Jarvis plus an inverse dot-gain
curve on MEMENTO camera and phone shots: Atkinson kept faces cleanest; the others looked muddy or wormy. dot_diameter 1.35
is a guess until the calibration strip (tools/calibration_strip.py) is measured."""
import numpy as np
from PIL import Image, ImageFilter

SS = 8  # subpixels per dot


def disk_offsets(radius: float):
    r = int(np.ceil(radius))
    return [(dy, dx) for dy in range(-r, r + 1) for dx in range(-r, r + 1) if dy * dy + dx * dx <= radius * radius]


def simulate(bw: Image.Image, dot_diameter: float = 1.35, blur: float = 0.6) -> Image.Image:
    """bw: mode 1 (0 = ink). dot_diameter in dot pitches."""
    ink = np.asarray(bw.convert("L")) < 128
    h, w = ink.shape
    centers = np.zeros((h * SS, w * SS), dtype=bool)
    centers[SS // 2::SS, SS // 2::SS] = ink
    covered = np.zeros_like(centers)
    for dy, dx in disk_offsets(dot_diameter * SS / 2):
        src = centers[max(0, -dy):h * SS - max(0, dy), max(0, -dx):w * SS - max(0, dx)]
        covered[max(0, dy):max(0, dy) + src.shape[0], max(0, dx):max(0, dx) + src.shape[1]] |= src
    cov = covered.reshape(h, SS, w, SS).mean(axis=(1, 3))
    paper, black = 0.92, 0.12            # reflectances: thermal paper isn't pure white, ink isn't pure black
    refl = paper - (paper - black) * cov
    img = Image.fromarray(np.clip(refl ** (1 / 2.2) * 255, 0, 255).astype(np.uint8), "L")
    return img.filter(ImageFilter.GaussianBlur(blur)) if blur else img


def coverage_curve(dot_diameter: float = 1.35, dither=None):
    """Printed coverage for uniform patches of each input coverage."""
    from ctp500 import raster
    out = []
    for i in range(0, 11):
        c = i / 10
        g = Image.new("L", (64, 64), round(255 * (1 - c)))
        bw = raster.to_1bit(g, dither or "floyd")
        ink = np.asarray(bw.convert("L")) < 128
        sim = np.asarray(simulate(bw, dot_diameter, 0), dtype=np.float32) / 255
        refl = sim ** 2.2
        out.append((c, float(ink.mean()), float((0.92 - refl.mean()) / 0.80)))
    return out
