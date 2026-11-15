"""A Windows PC's helper for the CTP500 print server (2026-11-02).

The Pi stays the hub; while this runs, it hands the PC the heavy jobs (ctp500/offload.py): the PC long-polls
GET /api/helper/next and answers POST /api/helper/result/<id>. It only calls out, so Windows needs no firewall
rule, and when the PC is off or asleep the Pi does everything itself as before.

Jobs:
    draw     an AI drawing (the Informative Drawings models) on the graphics card
    restore  faces redrawn sharp (restore.py, GFPGAN)
    slice    a lithophane STL -> a ready-to-print .gcode.3mf for the A1 mini, sliced by Bambu Studio's command line
             and checked (the A1 mini's own start G-code; every extruding move on the 180 mm bed)

Settings in desktop/helper.json (helper_settings.py). Runs from its own Python (<home>\\.venv: PyTorch +
onnxruntime-gpu), started at logon by the scheduled task "CTP500 helper" (desktop/install.ps1). Log:
<home>\\worker.log.

Memory (2026-11-13): the GPU stack commits ~1.3 GB just by being loaded (CUDA, cuDNN, cuBLAS), and onnxruntime's
GPU memory pool used to double whenever it ran short and never shrink (4 GB committed after 40 drawings of
different sizes, 7 GB after 80; the helper reached 3.8 GB over five days). So this process only polls and slices
and never loads it: drawings and face sharpening run in a child process (Gpu) that loads the models on its first
job, gives the pool back after every run, and exits after GPU_IDLE_S without one (first drawing ~1.1 s after a
start).
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from helper_settings import BAMBU_STUDIO as BAMBU, HELPER_TOKEN, HOME, SERVER, TOKEN  # noqa: E402
from ctp500 import gcode  # noqa: E402   (no numpy: this process stays small)

BBL = os.path.join(os.path.dirname(BAMBU), "resources", "profiles", "BBL")

log = logging.getLogger("helper")


# ---------------------------------------------------------------- GPU

GPU_IDLE_S = 600        # the GPU process exits after this long without a job (its libraries commit ~1.3 GB)
GPU_TIMEOUT_S = 120     # a job the GPU process hasn't answered by then: it's stopped and the job fails


def _torch_lib() -> str | None:
    """PyTorch's lib folder (its CUDA, cuBLAS and cuDNN DLLs), found without importing PyTorch: importing it
    committed another ~0.3 GB for nothing the helper uses."""
    import importlib.util
    spec = importlib.util.find_spec("torch")
    if spec is None or not spec.submodule_search_locations:
        return None
    lib = os.path.join(list(spec.submodule_search_locations)[0], "lib")
    return lib if os.path.isdir(lib) else None


def _gpu() -> list:
    """onnxruntime providers, the graphics card first (no separate CUDA install: PyTorch's DLLs are loaded)."""
    try:
        import onnxruntime as ort
        if "CUDAExecutionProvider" not in ort.get_available_providers():   # set up with -Cpu: plain onnxruntime
            log.info("onnxruntime without CUDA: running on the processor")
            return ["CPUExecutionProvider"]
        if hasattr(ort, "preload_dlls"):
            lib = _torch_lib()
            ort.preload_dlls(directory=lib) if lib else ort.preload_dlls()
            if lib:     # cuDNN's runtime-compiled kernels look for NVRTC by name ("Could not locate nvrtc64_120_0.dll")
                import ctypes
                import glob
                for pattern in ("nvrtc64_*.dll", "nvrtc-builtins64_*.dll", "nvJitLink_*.dll"):
                    for dll in glob.glob(os.path.join(lib, pattern)):
                        if not dll.endswith(".alt.dll"):
                            ctypes.WinDLL(dll)
        # every photo shape is a new input size: the default exhaustive cuDNN search re-ran per size (0.37 s
        # instead of 0.05 s for a drawing). The pool grows by what a run needs instead of doubling.
        return [("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC",
                                           "arena_extend_strategy": "kSameAsRequested"}), "CPUExecutionProvider"]
    except Exception as exc:
        log.warning("no GPU for onnxruntime: %s", exc)
    return ["CPUExecutionProvider"]


def _run_options():
    """Give onnxruntime's memory pools back after each run (GPU allocations count as committed memory on
    Windows): flat at ~1.4 GB over 80 drawings instead of 7 GB, ~10 ms slower a drawing."""
    import onnxruntime as ort
    opts = ort.RunOptions()
    opts.add_run_config_entry("memory.enable_memory_arena_shrinkage", "cpu:0;gpu:0")
    return opts


def _gpu_main(conn, log_file: str) -> None:
    """The GPU process: runs draw and restore jobs sent over conn until told to stop (None), the helper goes away,
    or nothing comes for GPU_IDLE_S."""
    threading.current_thread().name = "gpu"
    logging.basicConfig(filename=log_file, level=logging.INFO, format="%(asctime)s %(threadName)s %(message)s")
    from ctp500 import ai
    import restore
    ai.PROVIDERS = restore.PROVIDERS = _gpu()
    if ai.PROVIDERS[0] != "CPUExecutionProvider":
        ai.RUN_OPTIONS = restore.RUN_OPTIONS = _run_options()
    log.info("GPU process up (pid %s, onnxruntime %s)", os.getpid(), ai.PROVIDERS[0])
    idle = time.monotonic()
    while True:
        try:
            if not conn.poll(60):
                restore.drop_idle()                         # GFPGAN goes after 15 idle minutes, as before
                if time.monotonic() - idle > GPU_IDLE_S:
                    break
                continue
            msg = conn.recv()
        except (EOFError, OSError):                          # the helper is gone
            break
        if msg is None:
            break
        kind, data, params = msg
        try:
            out, meta = JOBS[kind](data, params)
            conn.send((True, out, meta))
        except Exception as exc:
            log.exception("%s failed in the GPU process", kind)
            conn.send((False, b"", str(exc)))
        idle = time.monotonic()
    log.info("GPU process stopping (pid %s)", os.getpid())


class Gpu:
    """The helper's side of the GPU process: started on the first draw or restore job, stopped after GPU_IDLE_S
    without one (or after a hang), started again by the next job."""

    def __init__(self, log_file: str):
        import multiprocessing
        self.ctx = multiprocessing.get_context("spawn")
        self.log_file = log_file
        self.lock = threading.Lock()
        self.proc = self.conn = None
        self.used = 0.0

    def _start(self) -> None:
        self.conn, child = self.ctx.Pipe()
        self.proc = self.ctx.Process(target=_gpu_main, args=(child, self.log_file), name="gpu", daemon=True)
        self.proc.start()
        child.close()

    def _stop(self, kill: bool = False) -> None:
        proc, conn, self.proc, self.conn = self.proc, self.conn, None, None
        if proc is None:
            return
        try:
            if not kill:
                conn.send(None)
                proc.join(10)
        except (OSError, ValueError):
            pass
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        conn.close()

    def run(self, kind: str, data: bytes, params: dict) -> tuple[bytes, dict]:
        with self.lock:
            self.used = time.monotonic()
            for attempt in (1, 2):        # a GPU process that stopped by itself just now: start a new one, once
                if self.proc is None or not self.proc.is_alive():
                    self._stop(kill=True)
                    self._start()
                try:
                    self.conn.send((kind, data, params))
                    if not self.conn.poll(GPU_TIMEOUT_S):
                        self._stop(kill=True)
                        raise RuntimeError(f"the GPU process didn't answer in {GPU_TIMEOUT_S} s")
                    ok, out, meta = self.conn.recv()
                    break
                except (EOFError, OSError):
                    self._stop(kill=True)
                    if attempt == 2:
                        raise RuntimeError("the GPU process stopped") from None
            self.used = time.monotonic()
        if not ok:
            raise RuntimeError(meta)
        return out, meta

    def stop_if_idle(self) -> bool:
        with self.lock:
            if self.proc is not None and time.monotonic() - self.used > GPU_IDLE_S:
                self._stop()
                return True
        return False


# ---------------------------------------------------------------- jobs

def job_draw(data: bytes, params: dict) -> tuple[bytes, dict]:
    from PIL import Image
    from ctp500 import ai
    picture = Image.open(io.BytesIO(data)).convert("RGB")
    t = time.perf_counter()
    drawing = ai.infer(params.get("model", "lineart"), picture)
    buf = io.BytesIO()
    drawing.save(buf, "PNG")
    return buf.getvalue(), {"seconds": round(time.perf_counter() - t, 3)}


def _profile(kind: str, name: str) -> dict:
    """A Bambu Studio system profile with its whole `inherits` chain and its `include` templates merged in: the CLI
    otherwise quietly uses defaults (without the templates it wrote a generic start G-code with a purge line off
    the A1 mini's bed, 2026-11-01)."""
    folder = os.path.join(BBL, kind)
    index = {}
    for f in os.listdir(folder):
        if f.endswith(".json"):
            try:
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    j = json.load(fh)
                index[j.get("name", f[:-5])] = j
            except (OSError, ValueError):
                pass
    chain, cur = [], index[name]
    while True:
        chain.append(cur)
        if not cur.get("inherits"):
            break
        cur = index[cur["inherits"]]
    merged: dict = {}
    for j in reversed(chain):
        merged.update(j)
        for inc in j.get("include", []):
            merged.update({k: v for k, v in index[inc].items() if k not in ("name", "instantiation", "type")})
    merged.pop("inherits", None)
    merged.pop("include", None)
    if kind != "machine":
        merged["compatible_printers"] = ["Bambu Lab A1 mini 0.4 nozzle"]
        merged["compatible_printers_condition"] = ""
    return merged


def _set(profile: dict, key: str, value) -> None:
    profile[key] = [str(value)] if isinstance(profile.get(key), list) else str(value)


# Quick lithophanes (litho.py): 0.24 mm Draft layers to match its terraces, solid (zig-zag fill, so no infill
# pattern shows in the light), one wall. Bambu Studio's estimate for 80 mm flat: 20 min + ~6 min of the printer's
# own start (heat, level, purge).
LITHO_PROCESS = {"sparse_infill_density": "100%", "sparse_infill_pattern": "zig-zag", "wall_loops": 1}


def check_gcode(text: str) -> None:
    """Refuse G-code that isn't safe to hand over: the A1 mini's own start must be there, and the printing part must
    stay on its bed (gcode.check_gcode, which the server runs again on what it's sent)."""
    with open(os.path.join(BBL, "machine", "Bambu Lab A1 mini 0.4 nozzle template machine_start_gcode.json"),
              encoding="utf-8") as fh:
        start = json.load(fh)["machine_start_gcode"]
    markers = [l.strip() for l in start.splitlines()
               if l.strip().startswith(("M1002", "G29.2", "M970", "M975")) and "{" not in l][:6]
    missing = [m for m in markers if m not in text]
    if not markers or missing:
        raise RuntimeError(f"not the A1 mini's start G-code (missing {missing[:2]})")
    try:
        gcode.check_gcode(text)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None


def job_slice(data: bytes, params: dict) -> tuple[bytes, dict]:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(params.get("name") or "lithophane"))[:60]
    shape = params.get("shape", "flat")
    work = tempfile.mkdtemp(prefix="ctp500-slice-")
    try:
        stl = os.path.join(work, f"{stem}-lithophane.stl")
        with open(stl, "wb") as fh:
            fh.write(data)
        if shape == "relief":                  # an opaque plaque: the A1 mini's own Standard profile as is
            process = _profile("process", "0.20mm Standard @BBL A1M")
        else:
            process = _profile("process", "0.24mm Draft @BBL A1M")
            for k, v in LITHO_PROCESS.items():
                _set(process, k, v)
        if shape == "curved":                  # stands on a thin edge: a brim keeps it down
            _set(process, "brim_type", "outer_only")
            _set(process, "brim_width", 5)
        files = {}
        for kind, profile in (("machine", _profile("machine", "Bambu Lab A1 mini 0.4 nozzle")), ("process", process),
                              ("filament", _profile("filament", "Bambu PLA Basic @BBL A1M"))):
            files[kind] = os.path.join(work, kind + ".json")
            with open(files[kind], "w", encoding="utf-8") as fh:
                json.dump(profile, fh)
        t = time.perf_counter()
        subprocess.run([BAMBU, "--slice", "0", "--orient", "0", "--arrange", "1",
                        "--curr-bed-type", "Textured PEI Plate",
                        "--load-settings", f"{files['machine']};{files['process']}",
                        "--load-filaments", files["filament"], "--outputdir", work, "--export-3mf", "out.3mf", stl],
                       timeout=280, creationflags=0x00004000 | 0x08000000)   # below normal, no window
        with open(os.path.join(work, "result.json"), encoding="utf-8") as fh:
            result = json.load(fh)
        if result.get("return_code") != 0:
            raise RuntimeError(f"Bambu Studio: {result.get('error_string')}")
        plate = result["sliced_plates"][0]
        out = io.BytesIO()
        with zipfile.ZipFile(os.path.join(work, "out.3mf")) as zin:
            check_gcode(zin.read("Metadata/plate_1.gcode").decode("utf-8", "replace"))
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    blob = zin.read(item.filename)
                    if item.filename == "Metadata/slice_info.config":   # the CLI leaves the model empty: A1 mini
                        blob = blob.replace(b'key="printer_model_id" value=""', b'key="printer_model_id" value="N1"')
                    zout.writestr(item, blob)
        return out.getvalue(), {"minutes": round(plate["total_predication"] / 60, 1),
                                "slice_seconds": round(time.perf_counter() - t, 1)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def job_restore(data: bytes, params: dict) -> tuple[bytes, dict]:
    """A photo with its faces redrawn sharp (restore.py; GFPGAN unless asked for another model)."""
    from PIL import Image
    import restore
    img = Image.open(io.BytesIO(data)).convert("RGB")
    fixed, n = restore.restore(img, params.get("model", "gfpgan_1.4"), float(params.get("blend", 1.0)))
    buf = io.BytesIO()
    fixed.save(buf, "JPEG", quality=95)
    return buf.getvalue(), {"faces": n}


JOBS = {"draw": job_draw, "slice": job_slice, "restore": job_restore}


# ---------------------------------------------------------------- polling

def _request(path: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 40):
    req = urllib.request.Request(SERVER + path, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("X-Helper-Token", HELPER_TOKEN)
    return urllib.request.urlopen(req, timeout=timeout)


GPU_JOBS = ("draw", "restore")
gpu: Gpu | None = None


def poll(kinds: list[str]) -> None:
    """One long-poll loop for these kinds of job (quick and slow jobs poll separately, so a 30 s slice never holds
    up a 0.1 s drawing). Drawings and face sharpening go to the GPU process, slices run here. Nothing ends it: a
    thread that died would leave the helper looking alive while the server counts it as gone."""
    refused = False
    while True:
        try:
            _poll_once(kinds)
            refused = False
        except urllib.error.HTTPError as exc:
            with exc:
                try:
                    reason = json.loads(exc.read())["error"]
                except Exception:
                    reason = str(exc)
            if exc.code in (401, 403):          # say it once, not every minute
                if not refused:
                    log.error("the print server won't give this helper jobs (%s): %s", exc.code, reason)
                refused = True
                time.sleep(60)
            else:
                log.info("print server answered %s (%s); retrying", exc.code, reason)
                time.sleep(10)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            log.info("print server not reachable (%s); retrying", exc)       # off, restarting, mid-answer
            time.sleep(10)
        except Exception:
            log.exception("polling failed; retrying")
            time.sleep(10)


def _poll_once(kinds: list[str]) -> None:
    """Wait for one job (up to ~25 s), run it and send back the result or the error."""
    with _request(f"/api/helper/next?kinds={','.join(kinds)}&name={os.environ.get('COMPUTERNAME', 'PC')}",
                  timeout=40) as res:
        if res.status == 204:
            return
        job_id, kind = res.headers["X-Job-Id"], res.headers["X-Job-Kind"]
        params = json.loads(res.headers.get("X-Job-Params") or "{}")
        data = res.read()
    t = time.perf_counter()
    headers = {}
    try:
        out, meta = gpu.run(kind, data, params) if kind in GPU_JOBS else JOBS[kind](data, params)
        headers["X-Job-Meta"] = json.dumps(meta)
        log.info("%s %s done in %.2f s %s", kind, job_id, time.perf_counter() - t, meta)
    except Exception as exc:
        out = b""
        # one line: a header can't hold line breaks (onnxruntime's and Bambu Studio's messages have them)
        message = " ".join(str(exc).split()) or type(exc).__name__
        headers["X-Job-Error"] = message.encode("ascii", "replace").decode()[:300]
        log.exception("%s %s failed", kind, job_id)
    try:
        _request(f"/api/helper/result/{job_id}", out, headers, timeout=60).close()
    except Exception as exc:
        log.warning("couldn't send the result of %s: %s", job_id, exc)


def main() -> None:
    os.makedirs(HOME, exist_ok=True)
    logging.basicConfig(filename=os.path.join(HOME, "worker.log"), level=logging.INFO,
                        format="%(asctime)s %(threadName)s %(message)s")
    if sys.stdout is not None:                         # pythonw has none
        logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    if not SERVER:
        log.error("no print server: set \"server\" in %s (see helper.example.json)", os.path.join(HERE, "helper.json"))
        sys.exit(1)
    if not HELPER_TOKEN:
        log.error("no helper token: on the print server run python -m ctp500 helper-token and put it in %s as "
                  "\"helper_token\" (or run install.ps1 -HelperToken ...)", os.path.join(HERE, "helper.json"))
    global gpu
    os.environ.setdefault("CTP500_MODELS", os.path.join(os.path.dirname(HERE), "models"))
    # numpy's OpenBLAS committed ~370 MB of buffers for its 12 threads at import; the GPU process's numpy work is
    # small (inherited by it, the GPU process being started from here)
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    gpu = Gpu(os.path.join(HOME, "worker.log"))
    log.info("helper up: server %s, models %s; the GPU process starts with the first drawing", SERVER,
             os.environ["CTP500_MODELS"])
    # GFPGAN loads on the first face sharpening and goes after 15 idle minutes (restore.drop_idle): kept loaded,
    # it and an earlier 3D model held the helper at 10.7 GB committed (2026-11-03).
    quick = list(GPU_JOBS)
    slow = ["slice"] if os.path.exists(BAMBU) else []
    threads = [threading.Thread(target=poll, args=(kinds,), name=label, daemon=True)
               for label, kinds in (("quick", quick), ("slow", slow)) if kinds]
    for th in threads:
        th.start()
    while True:
        time.sleep(30)
        if gpu.stop_if_idle():
            log.info("GPU process stopped (idle %d min)", GPU_IDLE_S // 60)


if __name__ == "__main__":
    main()
