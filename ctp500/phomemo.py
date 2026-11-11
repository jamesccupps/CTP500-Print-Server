"""The Phomemo M02 Pro's commands: ESC/POS-shaped, with Phomemo's own 1F 11 settings and 1A replies.

A print job:

    1B 40                  initialize
    1F 11 02 nn            density (1, 2 or 4)
    1F 11 37 nn            density coefficient (100, or 150 for the darkest)
    1F 11 0B               (the phone app sends it; meaning not documented)
    1F 11 35 00            raster not compressed
    1D 76 30 00 wL wH hL hH <rows>   GS v 0 raster, 72 bytes (576 dots) a row, MSB = leftmost, 1 = black
                           (on 50 mm paper the print uses dots 4..540 of them: printers.M02PRO_50)
    1B 4A nn               feed nn dots (extra feed, when asked for)
    1B 64 02  1B 64 02     print and feed two lines, twice (the phone app's ending)

Questions (1F 11 xx) are answered with 1A frames, and the printer sends some by itself (paper out, lid open).
As a real M02 Pro (firmware 0.0.4) answered them, one at a time:

    1F 11 11 -> 1A 06 88 out of paper (also when the lid is open) / 89 paper in
    1F 11 12 -> 1A 05 99 lid open / 98 closed (stayed 98 with the lid open: the paper flag says it)
    1F 11 07 -> 1A 07 a b c firmware
    1F 11 08 -> 1A 04 nn battery percent (A1-A3: low)
    1F 11 0E -> 1A 09 nn auto-off, nn x 5 minutes
    1A 03 A9 too hot / A8 cooled, 1A 0F 0C printed (after some prints), 1A 0B B8 cancelled on the printer
    on connect: 1A 3B 04 04 00 00 00 (what it can do)

Sources: TiMini-Print's phomemo_esc family, "m02_pro" recipe (Apache-2.0; the job layout, density levels, reply
lengths and meanings), phomemo-tools (GPL, read only to cross-check: ESC @ + GS v 0 + ESC d for the M02 Pro at
300 dpi) and theacodes/phomemo_m02s (the M02S, same 576-dot raster). Checked on a real M02 Pro, 2026-11-11.
"""

from __future__ import annotations

import struct

ROW_DOTS = 576
TOP_ROWS = 4                    # blank rows the phone app puts above every print

INIT = b"\x1b\x40"
SETTING = b"\x1f\x11"           # + what (+ value): Phomemo's settings and questions
RASTER_PREFIX = b"\x1d\x76\x30\x00"
MAX_BLOCK_LINES = 255
FEED_DOTS = b"\x1b\x4a"         # + n: feed n dots
FEED_LINES = b"\x1b\x64"        # + n: print and feed n lines
ENDING = FEED_LINES + b"\x02" + FEED_LINES + b"\x02"

# darkness preset -> (level, density, coefficient)
DARKNESS = {"light": (1, 1, 100), "normal": (2, 2, 100), "dark": (3, 4, 100), "darkest": (4, 4, 150)}
DEFAULT_DARKNESS = DARKNESS["normal"]
_BY_LEVEL = {v[0]: v for v in DARKNESS.values()}

PAPER_QUERY = b"\x1f\x11\x11"
LID_QUERY = b"\x1f\x11\x12"
FIRMWARE_QUERY = b"\x1f\x11\x07"
# Asked one at a time on a real M02 Pro (firmware 0.0.4, 2026-11-11): 1F 11 08 -> 1A 04 (battery %, 0x64 = 100),
# 1F 11 0E -> 1A 09 (auto-off, 0x0C = 60 min), 1F 11 09 -> 1A 08 (the serial number: never asked, it'd end up
# in /api/status), 1F 11 38 -> 1A 17 (chip). TiMini-Print's list pairs them differently (0E with the battery);
# replies are read by their opcode, so only the question matters. On connect it says 1A 3B 04 04 00 00 00.
BATTERY_QUERY = b"\x1f\x11\x08"
READY_QUERIES = PAPER_QUERY + LID_QUERY
STATUS_QUERIES = PAPER_QUERY + LID_QUERY + FIRMWARE_QUERY + BATTERY_QUERY

# 1A <opcode> <value bytes>: how many value bytes each opcode carries (unknown opcodes end the frame)
REPLY_FRAME = 0x1A
REPLY_LENGTH = {0x03: 1, 0x04: 1, 0x05: 1, 0x06: 1, 0x07: 3, 0x08: 15, 0x09: 1, 0x0B: 1, 0x0E: 1, 0x0F: 1,
                0x16: 1, 0x17: 1, 0x1D: 1, 0x20: 1, 0x35: 1, 0x3B: 5}
PAPER, LID, HEAT, BATTERY, FIRMWARE, RESULT, CANCELLED = 0x06, 0x05, 0x03, 0x04, 0x07, 0x0F, 0x0B
PRINTED_OK = 0x0C

# feed lines (as the settings count them, ~4 mm each) in dots at 300 dpi
LINE_DOTS = 47

# With its lid open (2026-11-11) the M02 Pro answered 1A 06 88 (paper out) and still 1A 05 98 (lid closed): like the
# CTP500, an open lid reads as no paper.
PROBLEM_TEXT = {"out_of_paper": "out of paper or the lid is open", "lid_open": "the lid is open", "overheated": "too hot",
                "low_battery": "the battery is low"}


def darkness_setting(darkness) -> tuple[int, int, int]:
    """(level 1..4, density, coefficient) from a preset name or a number (1..4 a level, bigger the 0..255
    scale the CTP500 settings use)."""
    if darkness is None or darkness in ("", "auto"):
        return DEFAULT_DARKNESS
    if isinstance(darkness, str):
        key = darkness.strip().lower()
        if key in DARKNESS:
            return DARKNESS[key]
        darkness = int(key)
    value = int(darkness)
    if value <= 5:
        level = max(1, min(4, value))
    else:
        level = 1 if value < 12 else 2 if value < 20 else 3 if value < 32 else 4
    return _BY_LEVEL[level]


def raster_blocks(rows: bytes, height: int, row_bytes: int) -> bytes:
    if len(rows) != row_bytes * height:
        raise ValueError(f"expected {row_bytes * height} bytes for {height} rows, got {len(rows)}")
    out = bytearray()
    for top in range(0, height, MAX_BLOCK_LINES):
        lines = min(MAX_BLOCK_LINES, height - top)
        out += RASTER_PREFIX + struct.pack("<HH", row_bytes, lines)
        out += rows[top * row_bytes:(top + lines) * row_bytes]
    return bytes(out)


def feed_command(dots: int) -> bytes:
    out = bytearray()
    dots = max(0, int(dots))
    while dots > 0:
        step = min(255, dots)
        out += FEED_DOTS + bytes([step])
        dots -= step
    return bytes(out)


def build_job(rows: bytes, height: int, row_bytes: int, darkness=None, feed: int = 0) -> bytes:
    """A complete job from packed rows (raster.pack: MSB = leftmost, 1 = black). feed: extra ~4 mm lines after
    the phone app's own ending."""
    _, density, coefficient = darkness_setting(darkness)
    rows = bytes(row_bytes * TOP_ROWS) + rows
    out = bytearray(INIT)
    out += b"\x1f\x11\x02" + bytes([density])
    out += b"\x1f\x11\x37" + bytes([coefficient])
    out += b"\x1f\x11\x0b"
    out += b"\x1f\x11\x35\x00"
    out += raster_blocks(rows, height + TOP_ROWS, row_bytes)
    out += feed_command(max(0, int(feed)) * LINE_DOTS)
    out += ENDING
    return bytes(out)


def print_seconds(height: int) -> float:
    """How long the printer takes for that many rows (TiMini-Print's estimate: ~9 mm a second). The M02 Pro
    doesn't say when it's done."""
    return (height + TOP_ROWS) * 0.0847 * 0.110


def parse_replies(data: bytes) -> list[tuple[int, bytes]]:
    """(opcode, value) for every complete 1A frame; anything else is skipped."""
    out = []
    i, n = 0, len(data)
    while i < n:
        if data[i] != REPLY_FRAME or i + 1 >= n:
            i += 1
            continue
        op = data[i + 1]
        size = REPLY_LENGTH.get(op)
        if size is None:
            i += 2
            continue
        if i + 2 + size > n:
            break
        out.append((op, bytes(data[i + 2:i + 2 + size])))
        i += 2 + size
    return out


def has(data: bytes, *opcodes: int) -> bool:
    found = {op for op, _ in parse_replies(data)}
    return all(op in found for op in opcodes)


def parse_state(reply: bytes) -> dict:
    """What the replies say, in the same shape as catproto.parse_state (problems: out_of_paper, lid_open,
    overheated, low_battery)."""
    info: dict = {"raw": reply.hex(" ")}
    flags: dict[str, bool] = {}
    for op, value in parse_replies(reply):
        v = value[0] if value else None
        if op == PAPER:
            flags["out_of_paper"] = v == 0x88
        elif op == LID:
            flags["lid_open"] = v == 0x99
        elif op == HEAT and v in (0xA8, 0xA9):
            flags["overheated"] = v == 0xA9
        elif op == BATTERY and v is not None:
            flags["low_battery"] = v in (0xA1, 0xA2, 0xA3)
            # to the nearest 10%: on the charger it wandered 75-100 from one question to the next (2026-11-11)
            info["battery"] = {0xA1: 10, 0xA2: 5, 0xA3: 3}.get(v, min(100, round(v / 10) * 10))
        elif op == FIRMWARE:
            info["firmware"] = ".".join(str(b) for b in value)
        elif op == RESULT:
            info["printed"] = v == PRINTED_OK
        elif op == CANCELLED and v == 0xB8:
            info["cancelled"] = True
    if flags:
        info["problems"] = [name for name, on in flags.items() if on]
    return info


def faults(data: bytes) -> list[str]:
    """The problems announced in data that stop a print (the printer sends these by itself mid-job)."""
    state = parse_state(data)
    return [p for p in state.get("problems", []) if p in ("out_of_paper", "lid_open")]
