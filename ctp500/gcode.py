"""Checking a ready-to-print file sliced on the PC helper (a .gcode.3mf for the Bambu Lab A1 mini) before it's handed
on: the server runs check_sliced on every file the helper sends back, and the helper runs check_gcode (plus its own
check of the start block against Bambu Studio's) before sending it. No numpy: the helper's main process imports it.
"""

from __future__ import annotations

import io
import re
import zipfile

BED_MM = 180                    # the A1 mini's bed, 180 x 180
MAX_GCODE_BYTES = 200 * 1024 * 1024


def check_gcode(gcode: str) -> None:
    """ValueError unless the printing part of sliced G-code (between the start and end blocks) stays on the bed:
    every extruding move inside 0..180 mm, no move below Z 0, all moves absolute (no G91). The printer's own
    time-lapse G-code parks the head at X-13 to take a frame: travel, allowed. The start and end blocks are the
    printer's own (relative moves, parking off the bed): the PC helper checks those against Bambu Studio's."""
    if "; MACHINE_START_GCODE_END" not in gcode or "; MACHINE_END_GCODE_START" not in gcode:
        raise ValueError("no start/end markers in the G-code")
    body = gcode.split("; MACHINE_START_GCODE_END", 1)[1].split("; MACHINE_END_GCODE_START", 1)[0]
    if re.search(r"^G91\b", body, re.M):
        raise ValueError("relative moves in the printing part")
    moves = [l for l in body.splitlines() if re.match(r"^G1 (?=.*X)(?=.*Y)(?=.*E\.?\d)", l)]
    if not moves:
        raise ValueError("no printing moves")
    xs = [float(re.search(r"X(-?[\d.]+)", l).group(1)) for l in moves]
    ys = [float(re.search(r"Y(-?[\d.]+)", l).group(1)) for l in moves]
    zs = [float(v) for v in re.findall(r"^G[01] .*?Z(-?[\d.]+)", body, re.M)]
    if min(xs) < 0 or max(xs) > BED_MM or min(ys) < 0 or max(ys) > BED_MM or (zs and min(zs) < 0):
        raise ValueError(f"moves off the bed: X {min(xs)}..{max(xs)} Y {min(ys)}..{max(ys)}")


def check_sliced(data: bytes) -> None:
    """ValueError unless data is a .gcode.3mf whose plate G-code passes check_gcode (the server runs this on
    every file the PC helper hands back before passing it on)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("Metadata/plate_1.gcode")
            if info.file_size > MAX_GCODE_BYTES:
                raise ValueError("the G-code is too big")
            gcode = z.read(info).decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, RuntimeError, NotImplementedError) as exc:
        raise ValueError(f"not a sliced 3MF ({exc})") from None
    check_gcode(gcode)
