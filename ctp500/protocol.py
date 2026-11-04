"""Byte-level commands for the Core Innovations CTP500 (InstaPrint/CorePrint dialect).

The printer speaks a small ESC/POS subset over Bluetooth Classic SPP (RFCOMM channel 1):

    1B 40                      reset/initialize
    1D 49 F0 nn                print darkness (nn: 0..255; the phone app sends 0x0F-0x19)
    1D 76 30 00 xL xH yL yH .. raster bit image, 48 bytes (384 dots) per row, MSB = leftmost dot
    0A                         feed one text line
    1E 47 03                   status request, answered with a short binary report

Sources: thirtythreedown/CTP500PrinterApp (sniffed phone traffic), parkerlreed's Linux gist,
Dejniel/TiMini-Print's "instaprint_core" family (chunk size and pacing).
"""

from __future__ import annotations

import struct

WIDTH = 384                    # dots per line (48 mm at 203 dpi)
ROW_BYTES = WIDTH // 8
DPI = 203

INIT = b"\x1b\x40"
DARKNESS_PREFIX = b"\x1d\x49\xf0"
RASTER_PREFIX = b"\x1d\x76\x30\x00"
STATUS_REQUEST = b"\x1e\x47\x03"
LINE_FEED = b"\x0a"

# Lines per GS v 0 block. The header holds 16 bits, but smaller blocks keep the printer's
# buffer happy on long prints and let a cancelled job stop sooner.
MAX_BLOCK_LINES = 256

DARKNESS_PRESETS = {"light": 8, "normal": 15, "dark": 25, "darkest": 40}
DEFAULT_DARKNESS = DARKNESS_PRESETS["normal"]

# RFCOMM pacing from TiMini-Print's CTP500 profile.
CHUNK_SIZE = 180
CHUNK_DELAY = 0.010


def darkness_value(darkness: int | str | None) -> int:
    """Map a preset name or number to the byte sent after 1D 49 F0."""
    if darkness is None or darkness in ("", "auto"):   # "auto" normally resolved by render.with_defaults
        return DEFAULT_DARKNESS
    if isinstance(darkness, str):
        key = darkness.strip().lower()
        if key in DARKNESS_PRESETS:
            return DARKNESS_PRESETS[key]
        darkness = int(key)
    return max(0, min(255, int(darkness)))


def darkness_command(darkness: int | str | None = None) -> bytes:
    return DARKNESS_PREFIX + bytes([darkness_value(darkness)])


def raster_blocks(rows: bytes, height: int) -> bytes:
    """Wrap packed 1-bit rows (ROW_BYTES each, 1 = black) in GS v 0 blocks."""
    if len(rows) != ROW_BYTES * height:
        raise ValueError(f"expected {ROW_BYTES * height} bytes for {height} rows, got {len(rows)}")
    out = bytearray()
    for top in range(0, height, MAX_BLOCK_LINES):
        lines = min(MAX_BLOCK_LINES, height - top)
        out += RASTER_PREFIX + struct.pack("<HH", ROW_BYTES, lines)
        out += rows[top * ROW_BYTES:(top + lines) * ROW_BYTES]
    return bytes(out)


def feed_command(lines: int) -> bytes:
    return LINE_FEED * max(0, int(lines))


def build_job(rows: bytes, height: int, darkness: int | str | None = None, feed: int = 4) -> bytes:
    """A complete print job: reset, darkness, the raster, then feed so the print clears the tear bar."""
    return INIT + darkness_command(darkness) + raster_blocks(rows, height) + feed_command(feed)


def parse_status(reply: bytes) -> dict:
    """Decode what we know of the status reply. The layout isn't documented, so the raw bytes
    always come along; fields get filled in as they're confirmed on real hardware."""
    info = {"raw": reply.hex(" "), "length": len(reply)}
    text = bytes(b for b in reply if 32 <= b < 127).decode("ascii", "replace").strip()
    if text:
        info["text"] = text
    return info
