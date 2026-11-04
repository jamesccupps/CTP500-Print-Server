"""The print server: a web app plus a small HTTP API, with a queue so prints never collide.

    GET  /                         the web app
    GET  /api/status               printer state, queue, version
    POST /api/check                connect and ask the printer for its status
    POST /api/print                JSON document (blocks / markup / text), queued
    POST /api/print/text           plain-text body (note markup), queued
    POST /api/print/image          raw image body (PNG/JPEG/...), queued
    POST /api/preview              same JSON as /api/print, answers with the PNG it would print
    POST /api/feed?lines=3         feed paper
    GET  /api/jobs                 recent jobs
    GET  /api/jobs/<id>.png        what a job printed
    POST /api/jobs/<id>/reprint
    POST /api/jobs/<id>/cancel

Print calls answer at once with the job; add ?wait=1 to answer after it has printed.
Options for every print call (JSON keys or query parameters): darkness, copies, feed.
"""

from __future__ import annotations

import base64
import collections
import hmac
import io
import itertools
import json
import mimetypes
import os
import queue
import sys
import threading
import time
import traceback
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from . import __version__, camera, catproto, config, litho, markup, offload, protocol, raster, render
from .printer import Printer, PrinterNotReady
from .transport import Cancelled, TransportError

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY = 40 * 1024 * 1024
CAMERA_OVERRIDES = ("style", "detail", "weight", "frame", "caption", "sideways", "zoom", "sharpen")
IMAGE_OPTIONS = ("dither", "style", "detail", "weight", "brightness", "contrast", "width", "rotate", "threshold",
                 "auto_contrast", "invert", "align")


class Job:
    _ids = itertools.count(1)

    def __init__(self, doc: dict, source: str, title: str):
        self.id = f"{time.strftime('%m%d%H%M%S')}-{next(self._ids)}"
        self.doc = doc
        self.source = source
        self.title = title
        self.created = time.time()
        self.status = "queued"         # queued | printing | waiting (for the printer) | done | failed | cancelled
        self.error: str | None = None
        self.progress = 0.0
        self.height = 0
        self.copies = max(1, min(20, int(doc.get("copies", 1))))
        self.darkness = doc.get("darkness")
        self.feed = int(doc.get("feed", 2))
        self.cancel = threading.Event()
        self.finished = threading.Event()
        self.image: Image.Image | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "source": self.source, "created": self.created,
            "status": self.status, "error": self.error, "progress": round(self.progress, 3),
            "height": self.height, "length_mm": round(self.height * 25.4 / protocol.DPI),
            "copies": self.copies, "darkness": self.darkness,
            "preview": f"/api/jobs/{self.id}.png",
        }


def _title(doc: dict) -> str:
    """A short name for the history list: the first words of text, else what the print is."""
    fallback = ""
    for block in doc.get("blocks", []):
        if not isinstance(block, dict):
            block = {"type": "text", "text": str(block)}
        kind = block.get("type", "text")
        value = block.get("text") or block.get("left")
        if not value and kind in ("list", "checklist") and block.get("items"):
            first = block["items"][0]
            value = first.get("text") if isinstance(first, dict) else first
        if value and str(value).strip():
            return str(value).strip().splitlines()[0][:60]
        if fallback:
            continue
        if kind == "image":
            fallback = block.get("name") or "Picture"
        elif kind == "qr":
            data = str(block.get("data", ""))
            if data.upper().startswith("WIFI:"):  # never show the password
                ssid = next((p[2:] for p in data[5:].split(";") if p.startswith("S:")), "")
                fallback = f"Wi-Fi QR ({ssid})" if ssid else "Wi-Fi QR"
            else:
                fallback = f"QR: {data[:50]}"
    return fallback or "Print"


class PrintService:
    PREVIEWS_KEPT = 48         # rendered camera-photo previews (PNG bytes, ~10-30 KB each)
    RETRY_EVERY = 20.0         # seconds between tries while a print waits for the printer

    def __init__(self, printer: Printer, cfg: dict):
        self.printer = printer
        self.cfg = cfg
        self.jobs_dir = os.path.join(cfg["data_dir"], "jobs")
        os.makedirs(self.jobs_dir, exist_ok=True)
        # history is shared by the HTTP threads and the print worker: always under _lock
        self._lock = threading.Lock()
        self.history: collections.OrderedDict[str, Job] = collections.OrderedDict()
        self.queue: queue.Queue[Job] = queue.Queue()
        self.started = time.time()
        self.camera = camera.CameraGallery(cfg["data_dir"], cfg.get("camera_stuck_pixel"))
        self._previews: collections.OrderedDict = collections.OrderedDict()
        self._drawing: dict = {}       # preview key -> Event, while it's being drawn
        self._predraw: queue.Queue[str] = queue.Queue()
        self._last_preview = 0.0       # when someone last asked for a preview (background drawing waits)
        self._trouble: str | None = None   # while prints wait for the printer: the alert sent about it
        self.offload = offload.Offload()   # heavy jobs go to the desktop PC while its helper runs
        self._ready_prints: collections.OrderedDict = collections.OrderedDict()
        self._sharpened: collections.OrderedDict = collections.OrderedDict()
        from . import ai
        ai.REMOTE = self._remote_drawing
        render.SHARPEN = self._remote_sharpen
        self._load_history()
        threading.Thread(target=self._worker, name="printer", daemon=True).start()
        threading.Thread(target=self._warm_up, name="warm-up", daemon=True).start()

    def _remote_drawing(self, name: str, picture: Image.Image) -> Image.Image | None:
        """An AI drawing from the PC's graphics card (~0.1 s instead of ~2.5 s here), or None to draw it here."""
        if not self.offload.online("draw"):
            return None
        buf = io.BytesIO()
        picture.save(buf, "PNG")                   # the model input, exactly (it's 8-bit to begin with)
        try:
            data, _ = self.offload.run("draw", buf.getvalue(), {"model": name}, timeout=15)
            return Image.open(io.BytesIO(data)).convert("L")
        except Exception as exc:
            print(f"draw on the PC: {exc}; drawing here", file=sys.stderr)
            return None

    SHARPEN_MODEL = "gfpgan_1.4"   # picked 2026-11-02 from GFPGAN / CodeFormer / RestoreFormer++ / GPEN on five
                                   # camera photos: the most natural faces, 0.12 s a photo on the PC

    def _remote_sharpen(self, picture: Image.Image) -> Image.Image | None:
        """The picture with its faces redrawn sharp on the PC (GFPGAN), or None when the PC is off. Kept for the
        last few pictures: previews and the print ask for the same one."""
        if not self.offload.online("restore"):
            return None
        rgb = picture.convert("RGB")
        key = (rgb.size, rgb.resize((32, 24), Image.BILINEAR).tobytes())
        with self._lock:
            if key in self._sharpened:
                self._sharpened.move_to_end(key)
                return self._sharpened[key].copy()
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=95)
        try:
            data, meta = self.offload.run("restore", buf.getvalue(), {"model": self.SHARPEN_MODEL}, timeout=20)
        except Exception as exc:
            print(f"sharpen faces on the PC: {exc}", file=sys.stderr)
            return None
        out = Image.open(io.BytesIO(data)).convert("RGB")
        with self._lock:
            self._sharpened[key] = out
            while len(self._sharpened) > 8:
                self._sharpened.popitem(last=False)
        return out.copy()

    def ready_print(self, name: str, size: float, shape: str) -> bytes:
        """A lithophane sliced for the A1 mini by Bambu Studio on the PC (its helper checks the G-code), as a
        .gcode.3mf to open and print. offload.Unavailable when the PC isn't on."""
        data = self.camera.read(name)
        key = (name, len(data), size, shape)
        with self._lock:
            if key in self._ready_prints:
                return self._ready_prints[key]
        t, res = litho.relief_heights(data, size) if shape == "relief" else litho.heights(data, size)
        stl = litho.stl(*litho.mesh(t, res, "flat" if shape == "relief" else shape), name=name)
        out, _ = self.offload.run("slice", stl, {"name": os.path.splitext(name)[0], "size": size, "shape": shape},
                                  timeout=300)
        with self._lock:
            self._ready_prints[key] = out
            while len(self._ready_prints) > 4:
                self._ready_prints.popitem(last=False)
        return out

    def _warm_up(self) -> None:
        """Load the AI model now rather than on the first preview (~2 s on the Pi), then draw the newest photos'
        previews in the background so the Photos page and Settings open on ready-made ones."""
        try:
            from . import ai
            if ai.available("lineart"):
                ai._session("lineart")
        except Exception:
            traceback.print_exc()
        for photo in self.camera.list()[:6]:
            self._predraw.put(photo["name"])
        self._predrawer()

    # ------------------------------------------------------------ jobs

    def submit(self, doc, source: str = "api", render_now: bool = True) -> Job:
        """Queue a print. render_now=False draws it in the print worker instead (camera prints: the camera
        shouldn't wait ~3 s for an AI drawing it never sees); render errors then fail the job."""
        doc = render.with_defaults(render.normalize(doc), self.cfg)
        job = Job(doc, source, _title(doc))
        if job.darkness is None:
            job.darkness = self.cfg.get("darkness")
        if "feed" not in doc:
            job.feed = int(self.cfg.get("feed", 2))
        if render_now:
            self._attach_image(job, render.render(doc))
        with self._lock:
            self.history[job.id] = job
            self._trim_history()
        self.queue.put(job)
        return job

    def _attach_image(self, job: Job, image) -> None:
        job.image = image
        job.height = image.height
        image.save(os.path.join(self.jobs_dir, f"{job.id}.png"))

    def reprint(self, job_id: str, source: str = "reprint") -> Job:
        old = self.job(job_id)
        if old is None:
            raise KeyError(job_id)
        doc = old.doc
        if doc is None:
            path = os.path.join(self.jobs_dir, f"{job_id}.json")
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        return self.submit(doc, source)

    def job(self, job_id: str) -> Job | None:
        with self._lock:
            return self.history.get(job_id)

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self.history.values())

    def cancel(self, job_id: str) -> Job:
        job = self.job(job_id)
        if job is None:
            raise KeyError(job_id)
        job.cancel.set()
        return job

    def _worker(self) -> None:
        while True:
            job = self.queue.get()
            try:
                self._run(job)
            except Exception:  # never let anything stop the queue
                traceback.print_exc()
                job.status, job.error = "failed", job.error or "internal error (see the server log)"
            job.image = None
            job.finished.set()
            self._save_job(job)

    def _run(self, job: Job) -> None:
        if job.cancel.is_set():
            job.status = "cancelled"
            return
        if job.image is None:
            try:
                self._attach_image(job, render.render(job.doc))
            except (render.RenderError, ValueError, OSError) as exc:
                job.status, job.error = "failed", f"couldn't draw it: {exc}"
                return
        # The printer can't take it (no paper, lid open, switched off, busy with the print box): the job waits
        # and tries again every RETRY_EVERY s for retry_minutes, and the phone hears about it once (notify_url).
        give_up = job.created + 60 * float(self.cfg.get("retry_minutes", 30) or 0)
        def sending(p, job=job):
            job.progress, job.status = p, "printing"

        while not job.cancel.is_set():
            if job.status != "waiting":      # a retry stays "waiting" until the printer takes data
                job.status = "printing"
            try:
                self.printer.print_image(job.image, job.darkness, job.feed, job.copies, job.cancel, sending)
                job.status, job.progress, job.error = "done", 1.0, None
                self._printer_ok()
                return
            except Cancelled:
                break
            except TransportError as exc:
                job.error, job.progress = str(exc), 0.0
                if time.time() + self.RETRY_EVERY > give_up:
                    job.status = "failed"
                    self._printer_gave_up(exc)
                    return
                job.status = "waiting"
                self._printer_trouble(exc)
                job.cancel.wait(self.RETRY_EVERY)
            except Exception as exc:  # never let one bad job stop the queue
                job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
                return
        job.status = "cancelled"
        if self.queue.empty():
            with self._lock:
                self._trouble = None    # nothing waits any more: the next problem is news again

    # ------------------------------------------------------------ printer trouble -> phone

    def _problem(self, exc: TransportError) -> tuple[str, str]:
        if isinstance(exc, PrinterNotReady):
            return "Printer: " + " and ".join(catproto.PROBLEM_TEXT.get(p, p) for p in exc.problems), ""
        return "Printer: can't reach it", "Is it switched on? "

    def _printer_trouble(self, exc: TransportError) -> None:
        title, hint = self._problem(exc)
        with self._lock:
            if self._trouble is not None:
                return
            self._trouble = title
        minutes = round(float(self.cfg.get("retry_minutes", 30)))
        self._notify(title, f"{hint}Waiting prints go out once it's fixed (for up to {minutes} minutes).")

    def _printer_ok(self) -> None:
        with self._lock:
            was, self._trouble = self._trouble, None
        if was:
            self._notify("Printer: printed after all", "The waiting prints went out.")

    def _printer_gave_up(self, exc: TransportError) -> None:
        if not self.queue.empty():
            return                  # the next print is still trying
        with self._lock:
            was, self._trouble = self._trouble, None
        title, hint = self._problem(exc)
        self._notify(title, (f"{hint}Gave up on the waiting prints; " if was else f"{hint}The print failed; ")
                     + "reprint from History in the web app.")

    def _notify(self, title: str, message: str) -> None:
        url = self.cfg.get("notify_url")
        if not url:
            return
        body = json.dumps({"title": title, "message": message}).encode()

        def send():
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).close()
            except Exception as exc:
                print(f"notify: {exc}", file=sys.stderr)
        threading.Thread(target=send, name="notify", daemon=True).start()

    # ------------------------------------------------------------ history on disk

    def _save_job(self, job: Job) -> None:
        try:
            with open(os.path.join(self.jobs_dir, f"{job.id}.json"), "w", encoding="utf-8") as f:
                json.dump(job.doc, f)
            job.doc = None  # reprints read it back from disk; keeps big pictures out of memory
            index = [j.as_dict() for j in self.jobs() if j.finished.is_set()]
            tmp = os.path.join(self.jobs_dir, "index.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(index, f)
            os.replace(tmp, os.path.join(self.jobs_dir, "index.json"))
        except OSError:
            traceback.print_exc()

    def _load_history(self) -> None:
        path = os.path.join(self.jobs_dir, "index.json")
        try:
            with open(path, encoding="utf-8") as f:
                entries = json.load(f)
        except (OSError, ValueError):
            return
        for entry in entries[-int(self.cfg.get("history", 100)):]:
            job = Job.__new__(Job)
            job.__dict__.update({
                "id": entry["id"], "doc": None, "source": entry.get("source", ""), "title": entry.get("title", ""),
                "created": entry.get("created", 0), "status": entry.get("status", "done"),
                "error": entry.get("error"), "progress": entry.get("progress", 1.0),
                "height": entry.get("height", 0), "copies": entry.get("copies", 1),
                "darkness": entry.get("darkness"), "feed": 4, "image": None,
                "cancel": threading.Event(), "finished": threading.Event(),
            })
            job.finished.set()
            self.history[job.id] = job

    def _trim_history(self) -> None:
        limit = max(10, int(self.cfg.get("history", 100)))
        while len(self.history) > limit:
            old_id, old = next(iter(self.history.items()))
            if not old.finished.is_set():
                break
            del self.history[old_id]
            for ext in (".png", ".json"):
                try:
                    os.remove(os.path.join(self.jobs_dir, old_id + ext))
                except OSError:
                    pass

    def status(self) -> dict:
        pending = [j.as_dict() for j in self.jobs() if j.status in ("queued", "printing", "waiting")]
        return {
            "version": __version__,
            "printer": self.printer.snapshot(),
            "queue": pending,
            "defaults": {"darkness": self.cfg.get("darkness"), "feed": self.cfg.get("feed")},
            "darkness_presets": protocol.DARKNESS_PRESETS,
            "width": protocol.WIDTH,
            "dpi": protocol.DPI,
            "auth": bool(self.cfg.get("token")),
            "uptime": round(time.time() - self.started),
            "helper": self.offload.status(),
        }

    # ------------------------------------------------------------ settings people change from the app

    # key -> (low, high) for numbers, a tuple of choices, bool, or str (max length)
    SETTINGS = {
        "padding_top_mm": (0.0, 50.0),
        "padding_bottom_mm": (0.0, 50.0),
        "photo_look": render.PHOTO_LOOKS,
        "camera_style": raster.PICTURE_MODES,
        "camera_detail": (0.0, 1.0),
        "camera_weight": (1.0, 3.0),
        "camera_frame": bool,
        "camera_sideways": bool,
        "camera_zoom": bool,
        "camera_sharpen": bool,
        "camera_caption": 60,
    }

    def settings(self) -> dict:
        return {key: self.cfg.get(key, config.DEFAULTS.get(key)) for key in self.SETTINGS}

    def update_settings(self, changes) -> dict:
        if not isinstance(changes, dict):
            raise ValueError("settings must be a JSON object")
        unknown = set(changes) - set(self.SETTINGS)
        if unknown:
            raise ValueError(f"unknown setting: {', '.join(sorted(unknown))}")
        clean = {key: self._check_setting(key, value) for key, value in changes.items()}
        self.cfg.update(clean)
        if self.cfg.get("_path"):  # started from a settings file: keep the change across restarts
            config.save(self.cfg)
        return self.settings()

    def _check_setting(self, key: str, value):
        rule = self.SETTINGS[key]
        if rule is bool:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
            return value
        if isinstance(rule, int):
            if not isinstance(value, str) or len(value) > rule:
                raise ValueError(f"{key} must be text of at most {rule} characters")
            return value
        if isinstance(rule, tuple) and rule and isinstance(rule[0], str):
            if value not in rule:
                raise ValueError(f"{key} must be one of: {', '.join(rule)}")
            return value
        low, high = rule
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None
        if not low <= number <= high:
            raise ValueError(f"{key} must be between {low:g} and {high:g}")
        return int(number) if number == int(number) and high > 1 else round(number, 2)

    # ------------------------------------------------------------ camera

    def camera_print(self, name: str, overrides: dict | None = None, source: str = "camera") -> Job:
        """Drawn by the print worker: the caller (the camera, the Photos page) gets its answer at once."""
        return self.submit(camera.print_doc(self.camera.read(name), name, self.cfg, overrides), source,
                           render_now=False)

    def camera_preview(self, name: str, overrides: dict | None = None, background: bool = False) -> tuple[bytes, int]:
        """(PNG, height in dots) of how a photo prints. Kept in a small cache, so flipping back to a photo or a
        setting shows at once instead of being redrawn (an AI drawing is ~3 s on the Pi)."""
        if not background:
            self._last_preview = time.monotonic()
        data = self.camera.read(name)
        doc = render.with_defaults(camera.print_doc(data, name, self.cfg, overrides), self.cfg)
        shape = {k: v for k, v in doc["blocks"][0].items() if k != "data"}
        # placeholders expanded, so a {time} caption isn't served stale
        key = (name, len(data), render.expand(json.dumps([shape, doc.get("padding_top_mm"), doc.get("padding_bottom_mm"),
                                                           doc.get("darkness"), doc.get("photo_look")],
                                                          sort_keys=True)),
               shape.get("sharpen_faces") and self.offload.online("restore"))   # sharpened only while the PC is on
        while True:
            with self._lock:
                if key in self._previews:
                    self._previews.move_to_end(key)
                    return self._previews[key]
                busy = self._drawing.get(key)
                if busy is None:            # nobody is drawing this one: we do
                    self._drawing[key] = threading.Event()
                    break
            busy.wait(60)                   # the same preview is being drawn (a prefetch): share it
        try:
            image = render.render(doc)
            buf = io.BytesIO()
            image.save(buf, "PNG")
            out = (buf.getvalue(), image.height)
            with self._lock:
                self._previews[key] = out
                while len(self._previews) > self.PREVIEWS_KEPT:
                    self._previews.popitem(last=False)
            return out
        finally:
            with self._lock:
                self._drawing.pop(key).set()

    def predraw(self, name: str) -> None:
        """Draw a new photo's preview in the background, so it's ready when someone opens it."""
        self._predraw.put(name)

    def _predrawer(self) -> None:
        while True:
            name = self._predraw.get()
            # someone looking at previews goes first, and so does a print being drawn (the camera sends its
            # waiting photos right after a print: drawing them then would hold the print up)
            while time.monotonic() - self._last_preview < 3 or any(j.status == "queued" for j in self.jobs()):
                time.sleep(0.5)
            try:
                self.camera_preview(name, background=True)
            except KeyError:
                pass                                            # deleted meanwhile
            except Exception:
                traceback.print_exc()


# ---------------------------------------------------------------- HTTP

def _options_from_query(query: dict) -> dict:
    out = {}
    for key in ("darkness", "copies", "feed"):
        if key in query:
            out[key] = query[key]
    return out


SHAPES_3D = ("flat", "curved", "relief")   # lithophanes and relief plaques


def _product(shape: str) -> str:
    """The 3D file's name part: relief-plaque, lithophane-flat, lithophane-curved."""
    return "relief-plaque" if shape == "relief" else f"lithophane-{shape}"


def _coerce(value: str):
    low = value.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


class Handler(BaseHTTPRequestHandler):
    server_version = f"ctp500/{__version__}"
    service: PrintService = None  # set by serve()
    quiet = False

    # ------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):
        if not self.quiet:
            super().log_message(fmt, *args)

    def _send_file(self, path: str, ctype: str, filename: str, remove: bool = False) -> None:
        """Stream a file from disk as a download (big zips never sit in memory)."""
        try:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(1 << 16)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        finally:
            if remove:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        # extra replaces the defaults (two Cache-Control headers made browsers obey the stricter one, so photo
        # thumbnails were never cached)
        headers = {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                   **(extra or {})}
        if code == 304:
            del headers["Content-Length"]
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and code != 304:
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _read_body(self) -> None:
        """Read the whole body up front: answering without reading it makes Windows reset the connection,
        so the caller would never see the error message."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.close_connection = True
            raise ValueError(f"request is too big ({length // 1048576} MB; the limit is {MAX_BODY // 1048576} MB)")
        self._raw = self.rfile.read(length) if length > 0 else b""

    def _body(self) -> bytes:
        return self._raw

    def _json_body(self):
        raw = self._body()
        if not raw:
            raise ValueError("empty request body")
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"body isn't valid JSON: {exc}") from None

    def _authorized(self, query: dict) -> bool:
        token = self.service.cfg.get("token")
        if not token:
            return True
        given = query.get("token", "")
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        return hmac.compare_digest(str(given), str(token))

    def _same_origin(self) -> bool:
        """Browsers send Origin on cross-site posts; scripts and Home Assistant don't. Refusing foreign
        origins stops any web page you visit from printing on your printer."""
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return origin is None
        return urlsplit(origin).netloc.lower() == (self.headers.get("Host") or "").lower()

    def _parse(self):
        parts = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        return parts.path.rstrip("/") or "/", query

    # ------------------------------------------------------------ verbs

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, query = self._parse()
        if not path.startswith("/api/"):
            return self._static(path)
        if not self._authorized(query):
            return self._error(401, "this printer needs its access token")
        svc = self.service
        if path == "/api/status":
            return self._json(200, svc.status())
        if path == "/api/settings":
            return self._json(200, svc.settings())
        if path == "/api/camera/photos":
            return self._json(200, {"photos": svc.camera.list()})
        if path == "/api/camera/photos.zip":
            return self._send_file(svc.camera.zip_all(), "application/zip",
                                   time.strftime("photos-%Y-%m-%d.zip"), remove=True)
        if path.startswith("/api/camera/litho/"):
            return self._lithophane(path[len("/api/camera/litho/"):], query)
        if path == "/api/helper/next":     # the PC helper's long poll (offload.py)
            job = svc.offload.next([k for k in query.get("kinds", "").split(",") if k], query.get("name", ""),
                                   min(25.0, float(query.get("wait", 25))))
            if job is None:
                return self._send(204, b"", "text/plain")
            return self._send(200, job["data"], "application/octet-stream", {
                "X-Job-Id": job["id"], "X-Job-Kind": job["kind"], "X-Job-Params": json.dumps(job["params"])})
        for prefix, reader in (("/api/camera/photo/", svc.camera.read), ("/api/camera/thumb/", svc.camera.thumb)):
            if path.startswith(prefix):
                name = path[len(prefix):]
                try:
                    # the list's URLs carry ?v=<modified time>, so a cached copy can never be stale
                    extra = {"Cache-Control": "private, max-age=31536000, immutable" if "v" in query else "no-cache"}
                    if query.get("download") in ("1", "true", "yes"):
                        extra["Content-Disposition"] = f'attachment; filename="{name}"'
                    return self._send(200, reader(name), "image/jpeg", extra)
                except KeyError:
                    return self._error(404, "no such photo")
        if path == "/api/jobs":
            return self._json(200, {"jobs": [j.as_dict() for j in reversed(svc.jobs())]})
        if path.startswith("/api/jobs/") and path.endswith(".png"):
            job_id = path[len("/api/jobs/"):-4]
            file = os.path.join(svc.jobs_dir, f"{job_id}.png")
            if "/" in job_id or "\\" in job_id or not os.path.exists(file):
                return self._error(404, "no such job")
            with open(file, "rb") as f:   # a job's picture never changes
                return self._send(200, f.read(), "image/png", {"Cache-Control": "private, max-age=31536000, immutable"})
        if path.startswith("/api/jobs/"):
            job = svc.job(path[len("/api/jobs/"):])
            return self._json(200, job.as_dict()) if job else self._error(404, "no such job")
        return self._error(404, "unknown API call")

    def _lithophane(self, rest: str, query: dict):
        """GET /api/camera/litho/<photo>.png (lit-from-behind preview), .stl (the 3D model) or .3mf (ready to print:
        sliced for the A1 mini on the PC); ?size= long side in mm, ?shape=flat|curved|relief."""
        name, _, fmt = rest.rpartition(".")
        try:
            size = float(query.get("size", litho.DEFAULT_SIZE))
            if not 30 <= size <= 170:
                raise ValueError("size must be 30 to 170 mm (the A1 mini's bed is 180 mm)")
            shape = query.get("shape", "flat")
            if fmt not in ("png", "stl", "3mf"):
                return self._error(404, "ask for .png (preview), .stl (the model) or .3mf (ready to print)")
            if shape not in SHAPES_3D:
                raise ValueError(f"shape must be one of {', '.join(SHAPES_3D)}")
            from . import ai
            if shape == "relief" and not ai.available("depth"):
                return self._error(503, "Relief plaques need the depth model: on the server, run "
                                        "python -m ctp500 models (and pip install onnxruntime)")
            if fmt == "3mf":
                try:
                    body = self.service.ready_print(name, size, shape)
                except offload.Unavailable:
                    return self._error(503, "Ready-to-print files are sliced by Bambu Studio on the PC: turn the PC "
                                            "on, or download the STL")
                stem = os.path.splitext(name)[0]
                return self._send(200, body, "application/vnd.ms-package.3dmanufacturing-3dmodel+xml", {
                    "Content-Disposition": f'attachment; filename="{stem}-{_product(shape)}-{size:g}mm.gcode.3mf"'})
            data = self.service.camera.read(name)
            t, res = litho.relief_heights(data, size) if shape == "relief" else litho.heights(data, size)
            w, h = litho.dimensions(t, res)
            if fmt == "png":
                png = litho.relief_preview(t, res) if shape == "relief" else litho.preview(t)
                return self._send(200, png, "image/png", {"X-Litho-Mm": f"{w:.0f} x {h:.0f}"})
            body = litho.stl(*litho.mesh(t, res, "flat" if shape == "relief" else shape), name=name)
            stem = os.path.splitext(name)[0]
            return self._send(200, body, "model/stl", {
                "Content-Disposition": f'attachment; filename="{stem}-{_product(shape)}-{size:g}mm.stl"'})
        except KeyError:
            return self._error(404, "no such photo")
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:
            traceback.print_exc()
            return self._error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self):
        path, query = self._parse()
        try:
            self._read_body()
        except ValueError as exc:
            return self._error(413, str(exc))
        if not self._same_origin():
            return self._error(403, "prints from other web sites aren't allowed")
        if not self._authorized(query):
            return self._error(401, "this printer needs its access token")
        try:
            return self._post(path, query)
        except render.RenderError as exc:
            return self._error(400, str(exc))
        except (ValueError, TypeError) as exc:
            return self._error(400, str(exc))
        except KeyError:
            return self._error(404, "no such job or photo")
        except TransportError as exc:
            return self._error(503, str(exc))
        except Exception as exc:
            traceback.print_exc()
            return self._error(500, f"{type(exc).__name__}: {exc}")

    def _post(self, path: str, query: dict):
        svc = self.service
        source = query.get("source") or ("web" if self.headers.get("X-Ctp500-Client") == "web" else "api")
        if path == "/api/print":
            doc = render.normalize(self._json_body())
            doc.update(_options_from_query(query))
            return self._queued(svc.submit(doc, source), query)
        if path == "/api/print/text":
            text = self._body().decode("utf-8-sig", "replace")
            if not text.strip():
                raise ValueError("nothing to print: the body is empty")
            size = int(query.get("size", 24))
            font = query.get("font", "sans")
            blocks = markup.parse(text, size=size, font=font) if query.get("markup", "1") != "0" else \
                [{"type": "text", "text": text, "size": size, "font": font, "align": query.get("align", "left")}]
            if "align" in query:
                for b in blocks:
                    b.setdefault("align", query["align"])
            doc = {"blocks": blocks}
            doc.update(_options_from_query(query))
            return self._queued(svc.submit(doc, source), query)
        if path == "/api/print/image":
            raw = self._body()
            if not raw:
                raise ValueError("send the picture as the request body")
            block = {"type": "image", "data": base64.b64encode(raw).decode("ascii"), "name": query.get("name", "Picture")}
            block.update({k: _coerce(v) for k, v in query.items() if k in IMAGE_OPTIONS})
            blocks = [block]
            if query.get("caption"):
                blocks.append({"type": "text", "text": query["caption"], "align": "center", "size": 22})
            doc = {"blocks": blocks}
            doc.update(_options_from_query(query))
            return self._queued(svc.submit(doc, source), query)
        if path == "/api/preview":
            image = render.render(render.with_defaults(render.normalize(self._json_body()), svc.cfg))
            buf = io.BytesIO()
            image.save(buf, "PNG")
            return self._send(200, buf.getvalue(), "image/png", {
                "X-Print-Height": str(image.height),
                "X-Print-Length-Mm": str(round(image.height * 25.4 / protocol.DPI)),
            })
        if path == "/api/settings":
            return self._json(200, svc.update_settings(self._json_body()))
        if path in ("/api/camera/upload", "/api/camera/print"):
            # the camera's own protocol: raw JPEG body, X-Filename; it counts 200/204 as done
            name = svc.camera.save(self._body(), self.headers.get("X-Filename"))
            if path.endswith("/upload"):
                svc.predraw(name)
                return self._json(200, {"name": name})
            overrides = {k: query[k] for k in CAMERA_OVERRIDES if k in query}
            return self._json(200, {"name": name, "job": svc.camera_print(name, overrides).as_dict()})
        if path.startswith("/api/camera/photo/") and path.endswith(("/print", "/delete", "/preview")):
            name = path[len("/api/camera/photo/"):].rsplit("/", 1)[0]
            if path.endswith("/delete"):
                svc.camera.delete(name)
                return self._json(200, {"deleted": name})
            body = self._json_body() if self._body() else {}
            overrides = {k: v for k, v in {**query, **(body if isinstance(body, dict) else {})}.items()
                         if k in CAMERA_OVERRIDES}
            if path.endswith("/preview"):   # what the paper would look like, without printing
                png, height = svc.camera_preview(name, overrides)
                return self._send(200, png, "image/png", {
                    "X-Print-Length-Mm": str(round(height * 25.4 / protocol.DPI))})
            return self._queued(svc.camera_print(name, overrides, source), query)
        if path.startswith("/api/helper/result/"):   # the PC helper's answer to a job
            meta = json.loads(self.headers.get("X-Job-Meta") or "{}")
            taken = svc.offload.result(path[len("/api/helper/result/"):], self._body(), meta,
                                       self.headers.get("X-Job-Error"))
            return self._json(200, {"taken": taken})
        if path == "/api/feed":
            svc.printer.feed(int(query.get("lines", 3)))
            return self._json(200, {"ok": True})
        if path == "/api/check":
            return self._json(200, {"status": svc.printer.check(), "printer": svc.printer.snapshot()})
        if path == "/api/disconnect":
            svc.printer.disconnect()
            return self._json(200, {"printer": svc.printer.snapshot()})
        if path.startswith("/api/jobs/") and path.endswith("/reprint"):
            return self._queued(svc.reprint(path.split("/")[3], "reprint"), query)
        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            return self._json(200, {"job": svc.cancel(path.split("/")[3]).as_dict()})
        return self._error(404, "unknown API call")

    def _queued(self, job: Job, query: dict):
        if query.get("wait") in ("1", "true", "yes"):
            until = time.monotonic() + float(query.get("timeout", 120))
            while not job.finished.wait(0.2) and job.status != "waiting" and time.monotonic() < until:
                pass                        # answers when it's printed, or waiting for the printer
            code = 200 if job.status == "done" else (202 if job.status in ("queued", "printing", "waiting") else 502)
            return self._json(code, {"job": job.as_dict()})
        return self._json(202, {"job": job.as_dict()})

    def _static(self, path: str):
        if path == "/":
            path = "/index.html"
        elif path == "/photos":
            path = "/photos.html"
        file = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not file.startswith(WEB_DIR + os.sep) or not os.path.isfile(file):
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(file)[0] or "application/octet-stream"
        if file.endswith(".webmanifest"):
            ctype = "application/manifest+json"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        # revalidated every time (edits show up on the next load), but unchanged files answer 304 with no body
        st = os.stat(file)
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        extra = {"Cache-Control": "no-cache", "ETag": etag}
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, b"", ctype, extra)
        with open(file, "rb") as f:
            self._send(200, f.read(), ctype, extra)


def serve(printer: Printer, cfg: dict, quiet: bool = False) -> ThreadingHTTPServer:
    """Start the server (call serve_forever() on the result)."""
    service = PrintService(printer, cfg)
    handler = type("BoundHandler", (Handler,), {"service": service, "quiet": quiet})
    httpd = ThreadingHTTPServer((cfg.get("host", "0.0.0.0"), int(cfg.get("port", 8500))), handler)
    httpd.daemon_threads = True
    httpd.service = service
    return httpd
