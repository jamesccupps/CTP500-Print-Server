"""Photos from the camera (an Adafruit MEMENTO running camera/code.py) and uploads: a small gallery on the
print server, and the layout camera prints use.

The camera uploads the way it always has (POST, Content-Type: image/jpeg, X-Filename, raw JPEG body; it counts
200/204 as done), so /api/camera/upload is a drop-in for its old desktop gallery server and
/api/camera/print is the same thing plus a print.
"""

from __future__ import annotations

import base64
import io
import os
import re
import time

from PIL import Image, ImageOps

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.jpe?g$", re.I)
THUMB_WIDTH = 240


class CameraGallery:
    def __init__(self, data_dir: str, stuck_pixel=None):
        self.stuck_pixel = stuck_pixel   # where the camera's sensor has a stuck pixel (fix_stuck_pixel), or None
        self.dir = os.path.join(data_dir, "camera")
        self.thumbs = os.path.join(self.dir, ".thumbs")
        os.makedirs(self.thumbs, exist_ok=True)
        self._taken: dict[str, tuple[float, float]] = {}   # name -> (mtime, taken), so EXIF is read once
        for entry in os.scandir(self.dir):   # zips left by a download the server never finished
            if entry.name.startswith(("photos-", "babycam-")) and entry.name.endswith(".zip"):
                try:
                    os.remove(entry.path)
                except OSError:
                    pass

    def path(self, name: str) -> str | None:
        """The file for a photo name, or None if the name isn't one of ours (no paths, no tricks)."""
        if not name or not _NAME.match(name):
            return None
        return os.path.join(self.dir, name)

    def save(self, data: bytes, name: str | None = None) -> str:
        """Store an upload and return its name. The same photo sent twice is kept once. JPEGs are kept exactly
        as sent; other pictures (PNG, WebP, ... from the photo browser) are stored as JPEG."""
        if not data:
            raise ValueError("send the photo as the request body")
        try:
            with Image.open(io.BytesIO(data)) as img:
                fmt = img.format
                img.verify()
        except Exception:
            raise ValueError("that isn't a picture the server can read") from None
        if fmt != "JPEG":
            with Image.open(io.BytesIO(data)) as img:
                out = io.BytesIO()
                ImageOps.exif_transpose(img).convert("RGB").save(out, "JPEG", quality=92)
                data = out.getvalue()
        else:
            data = fix_stuck_pixel(data, self.stuck_pixel)    # before the duplicate check: a re-sent shot patches the same way
        name = _clean_name(name)
        base, ext = os.path.splitext(name)
        candidate, n = name, 1
        while os.path.exists(os.path.join(self.dir, candidate)):
            if os.path.getsize(os.path.join(self.dir, candidate)) == len(data):
                with open(os.path.join(self.dir, candidate), "rb") as f:
                    if f.read() == data:
                        return candidate
            n += 1
            candidate = f"{base}-{n}{ext}"
        tmp = os.path.join(self.dir, f".{candidate}.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(self.dir, candidate))
        return candidate

    def list(self) -> list[dict]:
        """Newest first by when the photo was taken. "time" is when it reached the server, "taken" when it was
        taken (the camera's IMG_YYYYMMDD_HHMMSS name, the photo's EXIF date, else its arrival)."""
        out = []
        for entry in os.scandir(self.dir):
            if entry.is_file() and _NAME.match(entry.name):
                st = entry.stat()
                v = f"{int(st.st_mtime)}{st.st_size % 1000:03d}"   # changes if the name is ever reused
                out.append({"name": entry.name, "size": st.st_size, "time": st.st_mtime,
                            "taken": self._taken_time(entry.name, entry.path, st.st_mtime),
                            "url": f"/api/camera/photo/{entry.name}?v={v}",
                            "thumb": f"/api/camera/thumb/{entry.name}?v={v}",
                            "download": f"/api/camera/photo/{entry.name}?download=1"})
        out.sort(key=lambda p: (p["taken"], p["name"]), reverse=True)
        return out

    def _taken_time(self, name: str, path: str, mtime: float) -> float:
        cached = self._taken.get(name)
        if cached and cached[0] == mtime:
            return cached[1]
        taken = _time_from_name(name)
        if taken is None:
            try:
                with Image.open(path) as img:
                    exif = img.getexif()
                    stamp = exif.get_ifd(0x8769).get(36867) or exif.get(306)   # DateTimeOriginal, DateTime
                if stamp:
                    taken = time.mktime(time.strptime(str(stamp).strip()[:19], "%Y:%m:%d %H:%M:%S"))
            except Exception:
                taken = None
        taken = taken or mtime
        self._taken[name] = (mtime, taken)
        return taken

    def read(self, name: str) -> bytes:
        path = self.path(name)
        if not path or not os.path.isfile(path):
            raise KeyError(name)
        with open(path, "rb") as f:
            return f.read()

    def thumb(self, name: str) -> bytes:
        path = self.path(name)
        if not path or not os.path.isfile(path):
            raise KeyError(name)
        cached = os.path.join(self.thumbs, name)
        if not os.path.exists(cached) or os.path.getmtime(cached) < os.path.getmtime(path):
            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                img.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 2))
                img.save(cached, "JPEG", quality=80)
        with open(cached, "rb") as f:
            return f.read()

    def zip_all(self) -> str:
        """Every photo, as originally taken, in one zip (a temp file the caller removes). Stored, not
        compressed: JPEGs don't shrink, and the Pi stays quick."""
        import tempfile
        import zipfile

        fd, path = tempfile.mkstemp(prefix="photos-", suffix=".zip", dir=self.dir)
        os.close(fd)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            for photo in sorted(self.list(), key=lambda p: (p["time"], p["name"])):
                when = time.localtime(photo["time"])[:6]
                info = zipfile.ZipInfo(photo["name"], date_time=max(when, (1980, 1, 1, 0, 0, 0)))
                with open(os.path.join(self.dir, photo["name"]), "rb") as f:
                    z.writestr(info, f.read())
        return path

    def delete(self, name: str) -> None:
        path = self.path(name)
        if not path or not os.path.isfile(path):
            raise KeyError(name)
        os.remove(path)
        try:
            os.remove(os.path.join(self.thumbs, name))
        except OSError:
            pass


_STAMP = re.compile(r"(20\d\d)(\d\d)(\d\d)[_-]?(\d\d)(\d\d)(\d\d)")


def _time_from_name(name: str) -> float | None:
    """IMG_20261030_174205.jpg (the camera with its clock set), PXL_20261030_174205123.jpg (Pixel), ..."""
    m = _STAMP.search(name)
    if not m:
        return None
    try:
        return time.mktime(time.strptime("".join(m.groups()), "%Y%m%d%H%M%S"))
    except ValueError:
        return None


# A camera sensor can have a stuck (always bright) pixel cluster at the same place in every shot, at any size.
# Find it by averaging ~30 shots and set `camera_stuck_pixel` to [x, y] as shares of the width and height. This
# one is the MEMENTO it was written for (2026-10-31: 1024x768 at 911-912 x 309-313, 640x480 at 569 x 195).
STUCK_PIXEL = (0.889, 0.405)


def fix_stuck_pixel(data: bytes, where=None) -> bytes:
    """The photo with the camera's stuck pixel at `where` (x, y shares) painted out (from a small median around it), saved with its own
    JPEG settings (quality "keep": no visible loss). Unchanged unless it's camera-shaped (4:3, up to 1600 wide)
    and the bright spot is really there, so phone photos and clean shots stay byte for byte."""
    if not where:
        return data
    try:
        import numpy as np
        from PIL import ImageFilter
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if img.format != "JPEG" or img.mode != "RGB" or w > 1600 or abs(w * 3 - h * 4) > 4:
            return data
        img.load()
        r = max(3, round(6 * w / 1024))                # the spot is ~2x5 px at 1024 wide
        cx, cy = round(float(where[0]) * w), round(float(where[1]) * h)
        box = (cx - 3 * r, cy - 3 * r, cx + 3 * r + 1, cy + 3 * r + 1)   # wide enough that the fill never
        patch = img.crop(box)                                            # reaches its edge
        lum = np.asarray(patch.convert("L"), np.float32)
        excess = lum - np.asarray(patch.convert("L").filter(ImageFilter.MedianFilter(9)), np.float32)
        inner = excess[2 * r:4 * r + 1, 2 * r:4 * r + 1]
        if inner.max() < 35:                           # no bright spot there: leave the photo alone
            return data
        py, px = np.unravel_index(int(np.argmax(inner)), inner.shape)
        py, px = py + 2 * r, px + 2 * r
        yy, xx = np.mgrid[0:lum.shape[0], 0:lum.shape[1]]
        dist = np.hypot(yy - py, xx - px)
        hole = (dist <= 0.9 * r) | ((dist <= 1.6 * r) & (excess > 6))   # the spot and its JPEG glow
        # fill the hole smoothly from its edge (a few hundred rounds of neighbour averaging), so shading and
        # gradients carry across instead of a flat blot
        rgb = np.asarray(patch, np.float32).copy()
        rgb[hole] = rgb[~hole].mean(axis=0)
        for _ in range(300):
            avg = (np.roll(rgb, 1, 0) + np.roll(rgb, -1, 0) + np.roll(rgb, 1, 1) + np.roll(rgb, -1, 1)) / 4
            rgb[hole] = avg[hole]
        out = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
        img.paste(Image.fromarray(out, "RGB"), box[:2])
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality="keep", subsampling="keep")
        return buf.getvalue()
    except Exception:
        return data                                    # never lose a photo over this


def _clean_name(name: str | None) -> str:
    """A safe .jpg file name from whatever the uploader called it."""
    stem = os.path.splitext(os.path.basename(str(name or "").replace("\\", "/")))[0]
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._-")[:76]
    if not stem or not stem[0].isalnum():
        stem = time.strftime("IMG_%Y%m%d_%H%M%S")
    return stem + ".jpg"


def print_doc(photo: bytes, name: str, cfg: dict, overrides: dict | None = None) -> dict:
    """The print for a camera photo, from the camera settings (style, detail, frame, caption)."""
    opts = {"style": cfg.get("camera_style", "photoline"), "detail": cfg.get("camera_detail", 0.5),
            "weight": cfg.get("camera_weight", 2),
            "frame": cfg.get("camera_frame", True), "caption": cfg.get("camera_caption", "{date}"),
            "sideways": cfg.get("camera_sideways", True), "zoom": cfg.get("camera_zoom", False),
            "sharpen": cfg.get("camera_sharpen", False)}
    opts.update({k: v for k, v in (overrides or {}).items() if k in opts and v is not None})
    block = {"type": "image", "data": base64.b64encode(photo).decode("ascii"), "name": name,
             "style": opts["style"], "detail": float(opts["detail"]), "weight": int(float(opts["weight"]))}
    if _truthy(opts["frame"]):
        block["frame"] = True
        block["caption"] = str(opts["caption"] or "")
    if _truthy(opts["sideways"]):
        block["sideways"] = True    # landscape shots print along the paper, bigger
    if _truthy(opts["zoom"]):
        block["zoom_faces"] = True  # cropped to the people's heads and shoulders (faces.zoom_box)
    if _truthy(opts["sharpen"]):
        block["sharpen_faces"] = True   # faces redrawn sharp on the desktop PC's graphics card (render.SHARPEN)
    return {"blocks": [block]}


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)
