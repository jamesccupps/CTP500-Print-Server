"""Finding the printer's Bluetooth address (Linux: scan with BlueZ; Windows: list paired devices)."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

# Names the CTP500 and its clones advertise (from TiMini-Print's catalog, plus the model name itself).
NAME_HINTS = ("CTP500", "CTP-500", "Mini Printer", "CorePrint", "Core Print", "Teal Printer", "Purple Printer", "B Pink Printer",
              "Cherry Printer", "Floral Printer", "Check Printer", "Smiley Printer", "Stone Printer",
              "P Pink Printer", "YHK")
_ADDR = re.compile(r"([0-9A-F]{2}(?::[0-9A-F]{2}){5})", re.I)


def looks_like_ctp500(name: str) -> bool:
    low = (name or "").lower()
    return any(low.startswith(h.lower()) or h.lower() in low for h in NAME_HINTS)


def _run(args: list[str], timeout: float) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return out.stdout
    except subprocess.TimeoutExpired as exc:
        return exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")


def scan(seconds: int = 12) -> list[dict]:
    """[{address, name, likely, paired}] for nearby (Linux) or paired (Windows) devices."""
    if sys.platform.startswith("linux"):
        return _scan_linux(seconds)
    if sys.platform == "win32":
        return _paired_windows()
    raise RuntimeError("finding printers works on Linux and Windows; give the address with --printer instead")


def _scan_linux(seconds: int) -> list[dict]:
    if not shutil.which("bluetoothctl"):
        raise RuntimeError("bluetoothctl isn't installed (sudo apt install bluez)")
    _run(["bluetoothctl", "--timeout", str(seconds), "scan", "on"], seconds + 5)
    paired = {m.group(1).upper() for m in _ADDR.finditer(_run(["bluetoothctl", "devices", "Paired"], 5))}
    found = []
    for line in _run(["bluetoothctl", "devices"], 5).splitlines():
        m = re.match(r"Device\s+([0-9A-F:]{17})\s+(.*)", line.strip(), re.I)
        if m:
            addr, name = m.group(1).upper(), m.group(2).strip()
            found.append({"address": addr, "name": name, "likely": looks_like_ctp500(name), "paired": addr in paired})
    found.sort(key=lambda d: (not d["likely"], d["name"].lower()))
    return found


def _paired_windows() -> list[dict]:
    ps = ("Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
          "Where-Object { $_.InstanceId -like 'BTHENUM\\DEV_*' } | "
          "ForEach-Object { $_.InstanceId + '|' + $_.FriendlyName }")
    out = _run(["powershell", "-NoProfile", "-Command", ps], 20)
    found = []
    for line in out.splitlines():
        if "|" not in line:
            continue
        inst, name = line.split("|", 1)
        m = re.search(r"DEV_([0-9A-F]{12})", inst, re.I)
        if not m:
            continue
        raw = m.group(1).upper()
        addr = ":".join(raw[i:i + 2] for i in range(0, 12, 2))
        found.append({"address": addr, "name": name.strip(), "likely": looks_like_ctp500(name), "paired": True})
    found.sort(key=lambda d: (not d["likely"], d["name"].lower()))
    return found


def pair(address: str) -> str:
    """Pair and trust on Linux so the server can reconnect without prompts. Returns bluetoothctl's output."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("pair the printer in Windows Settings > Bluetooth & devices instead")
    if not shutil.which("bluetoothctl"):
        raise RuntimeError("bluetoothctl isn't installed (sudo apt install bluez)")
    out = []
    for cmd in (["pair", address], ["trust", address]):
        out.append(_run(["bluetoothctl", "--timeout", "20", *cmd], 30).strip())
    return "\n".join(o for o in out if o)
