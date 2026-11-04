"""Models only the PC helper runs (they want a graphics card), kept in <home>\\models (helper_settings.HOME) and
checked against pinned SHA-256s on every download. ONNX files are data: loading one runs no code from it.

Face restoration (redraws a blurry face at 512 x 512 from five landmarks, then it's blended back): GFPGAN v1.4
(Tencent ARC, Apache-2.0), the ONNX export published by the FaceFusion project (github.com/facefusion/
facefusion-assets, release models-3.0.0). Picked 2026-11-02 over CodeFormer, RestoreFormer++ and GPEN: the most
natural faces, and CodeFormer and GPEN are for non-commercial use only.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request

from helper_settings import HOME

FACEFUSION = "https://github.com/facefusion/facefusion-assets/releases/download/models-3.0.0/"

MODELS = {   # name: (url, sha256); the first download matched FaceFusion's published CRC32 (2026-11-02)
    "gfpgan_1.4": (FACEFUSION + "gfpgan_1.4.onnx",
                   "accc4757b26bdb89b32b4d3500d4f79c9dff97c1dd7c7104bf9dcb95e3311385"),
}


def path(name: str) -> str:
    return os.path.join(HOME, "models", name + ".onnx")


def fetch(name: str) -> str:
    url, sha256 = MODELS[name]
    dest = path(name)
    if os.path.exists(dest) and (sha256 is None or _sha(dest) == sha256):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=300) as res, open(tmp, "wb") as out:
        while chunk := res.read(1 << 20):
            out.write(chunk)
    got = _sha(tmp)
    if sha256 is not None and got != sha256:
        os.remove(tmp)
        raise RuntimeError(f"{name}: SHA-256 {got}, expected {sha256}; not installed")
    os.replace(tmp, dest)
    return dest


def _sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    import sys
    import zlib
    for name in sys.argv[1:] or MODELS:
        p = fetch(name)
        with open(p, "rb") as f:
            crc = format(zlib.crc32(f.read()), "08x")
        print(name, os.path.getsize(p) // 2**20, "MB", "crc32", crc, "sha256", _sha(p))
