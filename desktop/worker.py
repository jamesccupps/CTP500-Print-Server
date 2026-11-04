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
"""

from __future__ import annotations

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
from helper_settings import BAMBU_STUDIO as BAMBU, HOME, SERVER, TOKEN  # noqa: E402

BBL = os.path.join(os.path.dirname(BAMBU), "resources", "profiles", "BBL")

log = logging.getLogger("helper")


# ---------------------------------------------------------------- GPU

def _gpu() -> list[str]:
    """onnxruntime providers, the graphics card first. PyTorch's CUDA libraries are loaded first so onnxruntime-gpu
    finds them (no separate CUDA install)."""
    try:
        try:
            import torch  # noqa: F401
        except ImportError:                 # set up with -Cpu: plain onnxruntime
            pass
        import onnxruntime as ort
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            log.info("onnxruntime without CUDA: running on the processor")
            return ["CPUExecutionProvider"]
        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
        if "CUDAExecutionProvider" in ort.get_available_providers():
            # every photo shape is a new input size: the default exhaustive cuDNN search re-ran per size (0.37 s
            # instead of 0.05 s for a drawing)
            return [("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC"}), "CPUExecutionProvider"]
    except Exception as exc:
        log.warning("no GPU for onnxruntime: %s", exc)
    return ["CPUExecutionProvider"]


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


def check_gcode(gcode: str) -> None:
    """Refuse G-code that isn't safe to hand over: the A1 mini's own start must be there, and every extruding move
    between the start and end blocks must stay on the 180 x 180 bed (its own time-lapse G-code parks the head at
    X-13 to take a frame: travel, allowed), with Z never below 0."""
    with open(os.path.join(BBL, "machine", "Bambu Lab A1 mini 0.4 nozzle template machine_start_gcode.json"),
              encoding="utf-8") as fh:
        start = json.load(fh)["machine_start_gcode"]
    markers = [l.strip() for l in start.splitlines()
               if l.strip().startswith(("M1002", "G29.2", "M970", "M975")) and "{" not in l][:6]
    missing = [m for m in markers if m not in gcode]
    if not markers or missing:
        raise RuntimeError(f"not the A1 mini's start G-code (missing {missing[:2]})")
    if "; MACHINE_START_GCODE_END" not in gcode or "; MACHINE_END_GCODE_START" not in gcode:
        raise RuntimeError("no start/end markers in the G-code")
    body = gcode.split("; MACHINE_START_GCODE_END", 1)[1].split("; MACHINE_END_GCODE_START", 1)[0]
    moves = [l for l in body.splitlines() if re.match(r"^G1 (?=.*X)(?=.*Y)(?=.*E\.?\d)", l)]
    if not moves:
        raise RuntimeError("no printing moves")
    xs = [float(re.search(r"X(-?[\d.]+)", l).group(1)) for l in moves]
    ys = [float(re.search(r"Y(-?[\d.]+)", l).group(1)) for l in moves]
    zs = [float(v) for v in re.findall(r"^G[01] .*?Z(-?[\d.]+)", body, re.M)]
    if min(xs) < 0 or max(xs) > 180 or min(ys) < 0 or max(ys) > 180 or (zs and min(zs) < 0):
        raise RuntimeError(f"moves off the bed: X {min(xs)}..{max(xs)} Y {min(ys)}..{max(ys)}")


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
    return urllib.request.urlopen(req, timeout=timeout)


def poll(kinds: list[str]) -> None:
    """One long-poll loop for these kinds of job (quick and slow jobs poll separately, so a 30 s slice never holds
    up a 0.1 s drawing)."""
    while True:
        try:
            with _request(f"/api/helper/next?kinds={','.join(kinds)}&name={os.environ.get('COMPUTERNAME', 'PC')}",
                          timeout=40) as res:
                if res.status == 204:
                    continue
                job_id, kind = res.headers["X-Job-Id"], res.headers["X-Job-Kind"]
                params = json.loads(res.headers.get("X-Job-Params") or "{}")
                data = res.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.info("print server not reachable (%s); retrying", exc)
            time.sleep(10)
            continue
        t = time.perf_counter()
        headers = {}
        try:
            out, meta = JOBS[kind](data, params)
            headers["X-Job-Meta"] = json.dumps(meta)
            log.info("%s %s done in %.2f s %s", kind, job_id, time.perf_counter() - t, meta)
        except Exception as exc:
            out = b""
            headers["X-Job-Error"] = str(exc).encode("ascii", "replace").decode()[:300]
            log.exception("%s %s failed", kind, job_id)
        try:
            _request(f"/api/helper/result/{job_id}", out, headers, timeout=60).close()
        except (urllib.error.URLError, OSError) as exc:
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
    os.environ.setdefault("CTP500_MODELS", os.path.join(os.path.dirname(HERE), "models"))
    from ctp500 import ai
    import restore
    ai.PROVIDERS = restore.PROVIDERS = _gpu()
    log.info("helper up: server %s, onnxruntime %s, models %s", SERVER, ai.PROVIDERS[0], os.environ["CTP500_MODELS"])
    for name in ("lineart", "opensketch", "faces"):   # small and used all the time: loaded now
        if ai.available(name):
            ai._session(name)
    # GFPGAN loads on the first face sharpening and goes after 15 idle minutes (restore.drop_idle): kept loaded,
    # it and an earlier 3D model held the helper at 10.7 GB committed (2026-11-03).
    quick = ["draw", "restore"]
    slow = ["slice"] if os.path.exists(BAMBU) else []
    threads = [threading.Thread(target=poll, args=(kinds,), name=label, daemon=True)
               for label, kinds in (("quick", quick), ("slow", slow)) if kinds]
    for th in threads:
        th.start()
    import gc
    while True:
        time.sleep(60)
        if restore.drop_idle():
            gc.collect()
            log.info("face models let go (idle)")


if __name__ == "__main__":
    main()
