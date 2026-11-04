"""The "cat printer" dialect (TiMini-Print calls it "tiny"), which most CTP500 units speak.

Every command is a framed packet:

    51 78 <cmd> 00 <len lo> <len hi> <payload> <crc8 of payload> FF

    A4 [30+n]      darkness level n = 1..5
    AF [lo hi]     heating energy
    BE [0|1]       image or text mode
    BD [n]         motor speed (also sent every 200 rows)
    A2 [48 bytes]  one row of dots, LSB = leftmost, 1 = black
    BF [runs]      one row, run-length coded: (color << 7) | count, used when shorter than A2
    A1 [lo hi]     feed paper by that many dots (ignored by the Mini Printer-1025; we feed blank rows)
    A3 [00]        device state; answered with 51 78 A3 01 ...
    A8 [00]        device info (firmware version text)
    AE [10] / [00] printer -> host: pause / resume sending (flow control)

The job layout follows TiMini-Print's "mini_printer" profile (energy 14000, speed 20), which matches the
"Mini Printer-1025" this was tested on.
"""

from __future__ import annotations

from . import protocol

PREFIX = b"\x51\x78"
DEVICE_STATE = 0xA3
DEVICE_INFO = 0xA8
FLOW = 0xAE

SPEED = 20
ROW_FEED_EVERY = 200

# darkness preset -> (level 1..5, energy)
DARKNESS = {"light": (2, 10000), "normal": (3, 14000), "dark": (4, 19000), "darkest": (5, 26000)}
DEFAULT_DARKNESS = DARKNESS["normal"]


def crc8(data: bytes) -> int:
    """CRC-8, polynomial 0x07, initial value 0."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def packet(cmd: int, payload: bytes) -> bytes:
    n = len(payload)
    return PREFIX + bytes([cmd & 0xFF, 0, n & 0xFF, n >> 8]) + payload + bytes([crc8(payload), 0xFF])


def parse_packets(data: bytes) -> list[tuple[int, bytes]]:
    """(cmd, payload) for every well-formed packet in a byte stream; garbage is skipped."""
    out = []
    i = 0
    while True:
        i = data.find(PREFIX, i)
        if i < 0 or i + 6 > len(data):
            return out
        n = data[i + 4] | (data[i + 5] << 8)
        end = i + 6 + n + 2
        if end > len(data):
            return out
        payload = data[i + 6:i + 6 + n]
        if data[end - 1] == 0xFF and data[end - 2] == crc8(payload):
            out.append((data[i + 2], payload))
            i = end
        else:
            i += 1


def darkness_setting(darkness) -> tuple[int, int]:
    """(level, energy) from a preset name, or a number: 1..5 is a level, bigger is the ESC/POS 0..255 scale."""
    if darkness is None or darkness in ("", "auto"):   # "auto" normally resolved by render.with_defaults
        return DEFAULT_DARKNESS
    if isinstance(darkness, str):
        key = darkness.strip().lower()
        if key in DARKNESS:
            return DARKNESS[key]
        darkness = int(key)
    value = int(darkness)
    if value <= 5:
        level = max(1, value)
    else:  # map the 0..255 scale onto the presets
        level = 2 if value < 12 else 3 if value < 20 else 4 if value < 32 else 5
    return level, {1: 8000, 2: 10000, 3: 14000, 4: 19000, 5: 26000}[level]


def _rle(row: bytes) -> bytes | None:
    """Run-length code one packed row (MSB-first, 1 = black). None when the raw row is shorter."""
    runs = bytearray()
    color, count = None, 0
    limit = protocol.ROW_BYTES
    for byte in row:
        for bit in range(7, -1, -1):
            px = (byte >> bit) & 1
            if px == color:
                count += 1
                continue
            if color is not None:
                while count > 127:
                    runs.append((color << 7) | 127)
                    count -= 127
                runs.append((color << 7) | count)
                if len(runs) > limit:
                    return None
            color, count = px, 1
    while count > 127:
        runs.append((color << 7) | 127)
        count -= 127
    runs.append((color << 7) | count)
    return bytes(runs) if len(runs) <= limit else None


_LSB = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def build_job(rows: bytes, height: int, darkness=None, feed_dots: int = 96, text_mode: bool = False) -> bytes:
    """A complete job from packed rows (as raster.pack makes them: MSB = leftmost, 1 = black).

    feed_dots of blank rows follow the print to push it past the tear bar. Blank rows rather than the A1 feed
    command: the Mini Printer-1025 ignores A1, so a print ended with A1 stayed half inside the printer."""
    if len(rows) != protocol.ROW_BYTES * height:
        raise ValueError(f"expected {protocol.ROW_BYTES * height} bytes for {height} rows, got {len(rows)}")
    feed_dots = max(0, int(feed_dots))
    rows += bytes(protocol.ROW_BYTES * feed_dots)
    height += feed_dots
    level, energy = darkness_setting(darkness)
    out = bytearray()
    out += packet(0xA4, bytes([0x30 + level]))
    out += packet(0xAF, energy.to_bytes(2, "little"))
    out += packet(0xBE, b"\x01" if text_mode else b"\x00")
    out += packet(0xBD, bytes([SPEED]))
    rb = protocol.ROW_BYTES
    for y in range(height):
        row = rows[y * rb:(y + 1) * rb]
        coded = _rle(row)
        if coded is not None:
            out += packet(0xBF, coded)
        else:
            out += packet(0xA2, row.translate(_LSB))
        if (y + 1) % ROW_FEED_EVERY == 0:
            out += packet(0xBD, bytes([SPEED]))
    out += packet(0xBD, b"\x00")
    out += packet(DEVICE_STATE, b"\x00")
    return bytes(out)


def feed_command(dots: int) -> bytes:
    """Advance the paper: a job of nothing but blank rows."""
    return build_job(b"", 0, feed_dots=dots)


def state_request() -> bytes:
    return packet(DEVICE_STATE, b"\x00")


def info_request() -> bytes:
    return packet(DEVICE_INFO, b"\x00")


# A3 reply, first byte: the cat-printer state bits (NaitLee's Cat-Printer; TiMini-Print's V5G family reads 01/09 as
# out of paper, 04 overheated, 08 low voltage). 0 = fine. On the test printer (2026-10-30) an open lid answers
# 01 too (the paper sensor sees nothing): "01 1b 28" lid open vs "00 0c 28" closed; 02 hasn't been seen.
STATE_PROBLEMS = ((0x01, "out_of_paper"), (0x02, "lid_open"), (0x04, "overheated"), (0x08, "low_battery"))
# the ones that mean nothing will come out (the rest are warnings)
BLOCKING = ("out_of_paper", "lid_open")
PROBLEM_TEXT = {"out_of_paper": "out of paper or the lid is open", "lid_open": "the lid is open", "overheated": "too hot",
                "low_battery": "the battery is low"}


def problems(flags: int) -> list[str]:
    known = [name for bit, name in STATE_PROBLEMS if flags & bit]
    if flags & ~0x0F:
        known.append(f"unknown_{flags:02x}")
    return known


def parse_state(reply: bytes) -> dict:
    """Decode what's known of the replies; the raw values come along (bytes 2-3 of A3 aren't understood)."""
    info: dict = {"raw": reply.hex(" ")}
    for cmd, payload in parse_packets(reply):
        if cmd == DEVICE_STATE and payload:
            info["state_flags"] = payload[0]
            info["state"] = payload.hex(" ")
            info["problems"] = problems(payload[0])
        elif cmd == DEVICE_INFO:
            text = "".join(chr(b) for b in payload if 32 <= b < 127)
            if text:
                info["firmware"] = text
    return info


def flow_signal(data: bytes) -> bool | None:
    """True = printer asked to pause, False = resume, None = no flow packet in data."""
    signal = None
    for cmd, payload in parse_packets(data):
        if cmd == FLOW and payload:
            signal = payload[0] != 0
    return signal
