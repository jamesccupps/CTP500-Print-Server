"""Settings file: %APPDATA%\\ctp500\\config.json on Windows, ~/.config/ctp500/config.json elsewhere."""

from __future__ import annotations

import json
import os

DEFAULTS = {
    "printer": "",          # the main printer: Bluetooth address, or "sim"
    "printer_model": "ctp500",  # its model: ctp500 or m02pro (printers.MODELS)
    "channel": 1,
    "protocol": "auto",     # auto, cat (51 78 packets) or escpos
    # more printers, each {"model": "m02pro", "address": "AA:BB:..." or "sim"} (+ optional "id", "name",
    # "channel", "feed"); prints pick one with "printer": "<id>"
    "printers": [],
    "camera_printer": "",   # the id of the printer camera prints go to ("": the main printer)
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
    "camera_sharpen": False,  # sharpen faces with a restoration model on the desktop PC (when its helper runs)
    "camera_stuck_pixel": None,  # [x, y] (shares of width, height) of a stuck sensor pixel to paint out
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
    cfg = json.loads(json.dumps(DEFAULTS))     # a deep copy: "printers" is a list
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


def printer_entries(cfg: dict) -> list[dict]:
    """Every printer the settings name, the main one first: [{id, model, address, channel, protocol, label,
    feed, paper}]. ids are unique: the model's key unless the entry names its own (a second of the same model gets
    -2). paper is the roll width in mm, None for the model's usual paper."""
    from . import printers
    out: list[dict] = []
    raw = []
    if cfg.get("printer"):
        main = {"model": cfg.get("printer_model") or "ctp500", "address": cfg["printer"],
                "channel": cfg.get("channel", 1), "protocol": cfg.get("protocol", "auto"),
                "paper": cfg.get("paper")}
        if printers.model(main["model"]) is printers.CTP500:
            main["feed"] = cfg.get("feed", 2)       # the "feed" setting is the CTP500's
        else:
            main["channel"] = None if main["channel"] == DEFAULTS["channel"] else main["channel"]
        raw.append(main)
    extra = cfg.get("printers") or []
    if not isinstance(extra, list):
        raise ValueError("printers must be a list of {model, address}")
    raw.extend(extra)
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("address"):
            raise ValueError(f"each printer needs at least an address: {entry!r}")
        model = printers.model(entry.get("model"))
        base = str(entry.get("id") or model.key).strip().lower()
        pid, n = base, 1
        while pid in seen:
            n += 1
            pid = f"{base}-{n}"
        seen.add(pid)
        channel = entry.get("channel")
        paper = entry.get("paper")
        if paper not in (None, ""):
            model.paper_for(paper)              # a width the model doesn't take is a settings mistake: say so now
            paper = int(paper)
        else:
            paper = None
        out.append({"id": pid, "model": model.key, "address": str(entry["address"]),
                    "channel": model.channel if channel in (None, "", "auto") else int(channel),
                    "protocol": entry.get("protocol") or "auto",
                    "label": str(entry.get("name") or model.short),
                    "feed": int(entry.get("feed", model.feed)), "paper": paper})
    return out


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
