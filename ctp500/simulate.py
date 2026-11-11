"""Reading a job's bytes back into the paper it would produce. Used by the simulated printer and
the tests, so anything the real printer would get can be checked on screen first."""

from __future__ import annotations

import struct

from PIL import Image

from . import catproto, phomemo, protocol, raster

FEED_DOTS = 32   # an ESC/POS line feed is about 4 mm at 203 dpi
PHOMEMO_LINE_DOTS = 50   # ESC d n on the M02 Pro: assumed ESC/POS's default 1/6 inch line (not measured)


class DecodeError(ValueError):
    pass


def decode(stream: bytes, dialect: str | None = None) -> tuple[Image.Image | None, dict]:
    """(paper image or None, info) for any dialect."""
    if stream.startswith(catproto.PREFIX):
        return decode_cat(stream)
    if dialect == "phomemo" or stream.startswith(phomemo.INIT + phomemo.SETTING):
        return decode_phomemo(stream)
    return decode_escpos(stream)


def decode_cat(stream: bytes) -> tuple[Image.Image | None, dict]:
    """info has 'level', 'energy', 'rows', 'rle_rows', 'feed_dots', 'state_requests'."""
    info = {"dialect": "cat", "level": None, "energy": None, "rows": 0, "rle_rows": 0, "feed_dots": 0,
            "state_requests": 0, "packets": 0}
    packets = catproto.parse_packets(stream)
    framed = sum(6 + len(p) + 2 for _, p in packets)
    if framed != len(stream):
        raise DecodeError(f"{len(stream) - framed} bytes aren't valid packets (bad CRC or framing)")
    lines = bytearray()
    width = protocol.ROW_BYTES
    unlsb = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))
    for cmd, payload in packets:
        info["packets"] += 1
        if cmd == 0xA4:
            info["level"] = payload[0] - 0x30
        elif cmd == 0xAF:
            info["energy"] = int.from_bytes(payload, "little")
        elif cmd == 0xA2:
            if len(payload) != width:
                raise DecodeError(f"row is {len(payload)} bytes, expected {width}")
            lines += payload.translate(unlsb)
            info["rows"] += 1
        elif cmd == 0xBF:
            bits = []
            for run in payload:
                bits += [run >> 7] * (run & 0x7F)
            if len(bits) != protocol.WIDTH:
                raise DecodeError(f"compressed row has {len(bits)} dots, expected {protocol.WIDTH}")
            lines += bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, protocol.WIDTH, 8))
            info["rows"] += 1
            info["rle_rows"] += 1
        elif cmd == 0xA1:
            dots = int.from_bytes(payload, "little")
            lines += bytes(width * dots)
            info["feed_dots"] += dots
        elif cmd == catproto.DEVICE_STATE:
            info["state_requests"] += 1
    height = len(lines) // width
    if not height:
        return None, info
    return raster.unpack(bytes(lines), height), info


def decode_escpos(stream: bytes) -> tuple[Image.Image | None, dict]:
    """info has 'darkness' (list), 'inits', 'status_requests', 'feeds' and 'rasters' (block count)."""
    info = {"dialect": "escpos", "darkness": [], "inits": 0, "status_requests": 0, "feeds": 0, "rasters": 0}
    strips: list[Image.Image] = []
    i, n = 0, len(stream)
    while i < n:
        if stream.startswith(protocol.INIT, i):
            info["inits"] += 1
            i += 2
        elif stream.startswith(protocol.DARKNESS_PREFIX, i):
            if i + 3 >= n:
                raise DecodeError("darkness command cut short")
            info["darkness"].append(stream[i + 3])
            i += 4
        elif stream.startswith(protocol.STATUS_REQUEST, i):
            info["status_requests"] += 1
            i += 3
        elif stream.startswith(protocol.RASTER_PREFIX, i):
            if i + 8 > n:
                raise DecodeError("raster header cut short")
            width_bytes, lines = struct.unpack_from("<HH", stream, i + 4)
            if width_bytes != protocol.ROW_BYTES:
                raise DecodeError(f"raster is {width_bytes * 8} dots wide; the printer is {protocol.WIDTH}")
            start, end = i + 8, i + 8 + width_bytes * lines
            if end > n:
                raise DecodeError(f"raster block wants {end - start} bytes, only {n - start} sent")
            strips.append(raster.unpack(stream[start:end], lines))
            info["rasters"] += 1
            i = end
        elif stream[i] == 0x0A:
            info["feeds"] += 1
            strips.append(Image.new("1", (protocol.WIDTH, FEED_DOTS), 1))
            i += 1
        else:
            raise DecodeError(f"unknown byte 0x{stream[i]:02x} at offset {i}")
    if not strips:
        return None, info
    paper = Image.new("1", (protocol.WIDTH, sum(s.height for s in strips)), 1)
    y = 0
    for s in strips:
        paper.paste(s, (0, y))
        y += s.height
    return paper, info


PHOMEMO_ARGS = {0x02: 1, 0x37: 1, 0x35: 1}   # 1F 11 settings that carry a value; the rest are bare


def decode_phomemo(stream: bytes) -> tuple[Image.Image | None, dict]:
    """The Phomemo M02 Pro's jobs (phomemo.build_job). info has 'density', 'coefficient', 'inits', 'rasters',
    'rows', 'width', 'feed_dots' (ESC J) and 'feed_lines' (ESC d)."""
    info = {"dialect": "phomemo", "density": None, "coefficient": None, "inits": 0, "rasters": 0, "rows": 0,
            "width": None, "feed_dots": 0, "feed_lines": 0, "settings": []}
    strips: list[tuple[str, int, bytes]] = []
    i, n = 0, len(stream)
    while i < n:
        if stream.startswith(phomemo.INIT, i):
            info["inits"] += 1
            i += 2
        elif stream.startswith(phomemo.SETTING, i):
            if i + 2 >= n:
                raise DecodeError("setting cut short")
            op = stream[i + 2]
            args = PHOMEMO_ARGS.get(op, 0)
            if i + 3 + args > n:
                raise DecodeError(f"setting 1f 11 {op:02x} cut short")
            value = stream[i + 3] if args else None
            info["settings"].append(op)
            if op == 0x02:
                info["density"] = value
            elif op == 0x37:
                info["coefficient"] = value
            i += 3 + args
        elif stream.startswith(phomemo.RASTER_PREFIX, i):
            if i + 8 > n:
                raise DecodeError("raster header cut short")
            width_bytes, lines = struct.unpack_from("<HH", stream, i + 4)
            if info["width"] not in (None, width_bytes * 8):
                raise DecodeError("raster blocks of different widths")
            info["width"] = width_bytes * 8
            start, end = i + 8, i + 8 + width_bytes * lines
            if end > n:
                raise DecodeError(f"raster block wants {end - start} bytes, only {n - start} sent")
            strips.append(("rows", lines, stream[start:end]))
            info["rasters"] += 1
            info["rows"] += lines
            i = end
        elif stream.startswith(phomemo.FEED_DOTS, i) or stream.startswith(phomemo.FEED_LINES, i):
            if i + 2 >= n:
                raise DecodeError("feed cut short")
            lines = stream[i + 2]
            if stream[i + 1] == 0x4A:
                info["feed_dots"] += lines
                strips.append(("feed", lines, b""))
            else:
                info["feed_lines"] += lines
                strips.append(("feed", lines * PHOMEMO_LINE_DOTS, b""))
            i += 3
        else:
            raise DecodeError(f"unknown byte 0x{stream[i]:02x} at offset {i}")
    if not info["rows"]:
        return None, info
    width = info["width"]
    parts = [raster.unpack(data, h, width) if kind == "rows" else Image.new("1", (width, h), 1)
             for kind, h, data in strips if h]
    paper = Image.new("1", (width, sum(p.height for p in parts)), 1)
    y = 0
    for p in parts:
        paper.paste(p, (0, y))
        y += p.height
    return paper, info
