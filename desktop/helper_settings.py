"""The PC helper's settings: desktop/helper.json (copy helper.example.json; it stays out of git), and environment
variables, which win: CTP500_SERVER, CTP500_TOKEN, CTP500_HELPER_HOME."""

from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def _load() -> dict:
    try:
        with open(os.path.join(HERE, "helper.json"), encoding="utf-8-sig") as fh:   # Notepad may add a BOM
            return json.load(fh)
    except FileNotFoundError:
        return {}


_cfg = _load()

SERVER = (os.environ.get("CTP500_SERVER") or _cfg.get("server") or "").rstrip("/")   # the print server's address
TOKEN = os.environ.get("CTP500_TOKEN") or _cfg.get("token") or ""                    # if the server has a token
# Its Python, models and log (a few GB with PyTorch: put it on a roomy drive)
HOME = (os.environ.get("CTP500_HELPER_HOME") or _cfg.get("home")
        or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ctp500-helper"))
BAMBU_STUDIO = _cfg.get("bambu_studio") or r"C:\Program Files\Bambu Studio\bambu-studio.exe"
