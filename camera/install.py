"""Set up the camera app on an Adafruit MEMENTO that already runs CircuitPython 10 (plug it in by USB first):

    python camera/install.py                       # finds the CIRCUITPY drive and asks what it needs
    python camera/install.py --server http://raspberrypi.local:8500 --wifi MyWiFi

It writes settings.toml (only if the camera has none yet: Wi-Fi, the print server, time zone), installs the
libraries with circup when it's there (pip install circup), and copies code.py last (the camera restarts by itself).
Any code.py already on the camera is kept as code-before-install.py.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LIBRARIES = ["adafruit_pycamera", "adafruit_ntp", "adafruit_requests"]   # circup brings their dependencies


def find_drive() -> str | None:
    """The CIRCUITPY drive: a drive with boot_out.txt at its root, named CIRCUITPY where the OS shows names."""
    if os.name == "nt":
        candidates = [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\boot_out.txt")]
    else:
        user = os.environ.get("USER", "")
        candidates = [p for p in ("/Volumes/CIRCUITPY", f"/media/{user}/CIRCUITPY", f"/run/media/{user}/CIRCUITPY")
                      if os.path.exists(os.path.join(p, "boot_out.txt"))]
    return candidates[0] if len(candidates) == 1 else None


def _toml(value) -> str:
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ask(question: str, default: str = "") -> str:
    answer = input(f"{question}{f' [{default}]' if default else ''}: ").strip()
    return answer or default


def _server_default() -> str:
    try:
        sys.path.insert(0, os.path.dirname(HERE))
        from ctp500 import config
        return config.load().get("server") or ""
    except Exception:
        return ""


def _needs_token(server: str) -> bool:
    try:
        with urllib.request.urlopen(server + "/api/status", timeout=5) as res:
            return False
    except urllib.error.HTTPError as exc:
        return exc.code == 401
    except Exception:
        print(f"  (couldn't reach {server} now; the camera will try once it's on Wi-Fi)")
        return False


def settings(args) -> str:
    server = (args.server or _ask("Print server address", _server_default() or "http://raspberrypi.local:8500"))
    server = server.rstrip("/")
    if not server.startswith("http"):
        server = "http://" + server
    token = args.token
    if token is None and _needs_token(server):
        token = getpass.getpass("The server wants its token: ")
    suffix = f"?token={token}" if token else ""
    wifi = args.wifi or _ask("Home Wi-Fi name")
    password = args.password if args.password is not None else getpass.getpass(f"Wi-Fi password for {wifi}: ")
    offset = -(time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone) / 3600
    standard = -time.timezone / 3600
    lines = [
        "# Written by camera/install.py; see settings.toml.example for every setting.",
        f"WIFI_SSID = {_toml(wifi)}",
        f"WIFI_PASSWORD = {_toml(password)}",
        f"CAMERA_SERVER = {_toml(server + '/api/camera/upload' + suffix)}",
        f"CAMERA_PRINTER = {_toml(server + '/api/camera/print' + suffix)}",
        "INSTANT_PRINT = 1",
        f"TZ_OFFSET = {int(standard) if standard == int(standard) else standard}",
    ]
    if time.daylight and -10 <= standard <= -4:          # North America: the US summer-time dates
        lines.append('TZ_DST = "us"')
    elif time.daylight:
        print(f"  Photo names use UTC{offset:+g}; the summer hour is only automatic for the US dates.")
    lines += ['CAMERA_NAME = "CAMERA"', "IDLE_DIM_AFTER = 90", "DEEP_SLEEP_AFTER = 0"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Set up the CTP500 camera app on an Adafruit MEMENTO.")
    ap.add_argument("--drive", help="the camera's CIRCUITPY drive (found by itself if there's one)")
    ap.add_argument("--server", help="the print server, e.g. http://raspberrypi.local:8500")
    ap.add_argument("--token", help="the server's token, if it has one")
    ap.add_argument("--wifi", help="home Wi-Fi name")
    ap.add_argument("--password", help="home Wi-Fi password (asked for if not given)")
    ap.add_argument("--no-libraries", action="store_true", help="don't run circup")
    args = ap.parse_args()

    drive = args.drive or find_drive()
    if not drive or not os.path.exists(os.path.join(drive, "boot_out.txt")):
        print("No CIRCUITPY drive found. Plug the camera in by USB (it must already run CircuitPython 10), or give "
              "--drive.", file=sys.stderr)
        return 1
    with open(os.path.join(drive, "boot_out.txt"), encoding="utf-8", errors="replace") as fh:
        boot = fh.read()
    print(f"Camera drive: {drive}  ({boot.splitlines()[0] if boot else 'boot_out.txt is empty'})")
    if "camera" not in boot.lower() and "memento" not in boot.lower():
        print("  Warning: this doesn't say it's a MEMENTO (Adafruit Camera); carrying on.")

    target = os.path.join(drive, "settings.toml")
    if os.path.exists(target) and "WIFI_SSID" in open(target, encoding="utf-8", errors="replace").read():
        print("settings.toml: already set up, left as it is (edit it on the drive to change anything)")
    else:
        text = settings(args)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print("settings.toml: written")

    if not args.no_libraries:
        circup = shutil.which("circup")
        if circup:
            print("Libraries: circup install " + " ".join(LIBRARIES))
            if subprocess.run([circup, "--path", drive, "install", *LIBRARIES]).returncode:
                print("  circup didn't finish; run it again, or copy the libraries by hand (camera/README.md).")
        else:
            print("Libraries: circup isn't installed. Either `pip install circup` and run this again, or copy the "
                  "libraries listed in camera/README.md into the drive's lib folder.")

    code = os.path.join(drive, "code.py")
    if os.path.exists(code):
        with open(code, "rb") as a, open(os.path.join(HERE, "code.py"), "rb") as b:
            same = a.read() == b.read()
        if not same:
            shutil.copyfile(code, os.path.join(drive, "code-before-install.py"))
    shutil.copyfile(os.path.join(HERE, "code.py"), code)
    print("code.py: copied. The camera restarts by itself; with a microSD card in it, it's ready to shoot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
