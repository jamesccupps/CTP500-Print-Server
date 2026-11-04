"""Settings file: %APPDATA%\\ctp500\\config.json on Windows, ~/.config/ctp500/config.json elsewhere."""

from __future__ import annotations

import json
import os

DEFAULTS = {
    "printer": "",          # Bluetooth address, or "sim"
    "channel": 1,
    "protocol": "auto",     # auto, cat (51 78 packets) or escpos
    "host": "0.0.0.0",
    "port": 8500,
    "token": "",            # when set, the API wants "Authorization: Bearer <token>" (or ?token=)
    "darkness": "normal",
    "photo_look": "punchy",  # photo tones: punchy (picked on paper, 2026-10-30) or even (measured, prints at light)
    "feed": 2,              # ~4 mm lines fed after the bottom padding: about the head-to-tear-bar distance
    "padding_top_mm": 6,    # blank paper above and below every print (a document can set its own)
    "padding_bottom_mm": 6,
    "camera_style": "photoline",  # how camera photos print (any picture style)
    "camera_detail": 0.5,
    "camera_weight": 2,     # outline strength (Photo + outlines) / line weight: 1 thin, 2 medium, 3 bold
    "camera_frame": True,    # instant-photo frame with a caption
    "camera_sideways": True,  # landscape shots print along the paper: ~1.4x bigger (turn the print to look)
    "camera_caption": "{date}",
    "camera_zoom": False,   # zoom camera prints in on the faces (up to 2x)
    "camera_sharpen": False,
    "camera_stuck_pixel": None,  # [x, y] (shares of width, height) of a stuck sensor pixel to paint out  # sharpen faces with a restoration model on the desktop PC (when its helper runs)
    "keep_alive": 45,       # seconds to stay connected after a job
    "server": "",           # for the command line: print through this server instead of Bluetooth
    "data_dir": "",
    "history": 100,         # jobs kept for the history list and reprints
    "retry_minutes": 30,    # a print the printer can't take (no paper, lid open, off) waits this long, retrying
    "notify_url": "",       # POST {"title", "message"} here when prints wait / go out after all (HA webhook)
}


def config_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ctp500")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "ctp500")


def default_path() -> str:
    return os.environ.get("CTP500_CONFIG") or os.path.join(config_dir(), "config.json")


def load(path: str | None = None) -> dict:
    cfg = dict(DEFAULTS)
    path = path or default_path()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"{path} should hold a JSON object")
        cfg.update({k: v for k, v in data.items() if not k.startswith("_")})
    for key in ("printer", "server", "token"):
        env = os.environ.get(f"CTP500_{key.upper()}")
        if env:
            cfg[key] = env
    if not cfg["data_dir"]:
        cfg["data_dir"] = os.path.join(config_dir(), "data")
    cfg["_path"] = path
    return cfg


def save(cfg: dict, path: str | None = None) -> str:
    path = path or cfg.get("_path") or default_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {k: v for k, v in cfg.items() if not k.startswith("_") and DEFAULTS.get(k) != v}
    if data.get("data_dir") == os.path.join(config_dir(), "data"):  # filled in by load(), not chosen
        del data["data_dir"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path
