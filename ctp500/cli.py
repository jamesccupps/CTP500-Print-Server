"""Command line: print, preview, find the printer, run the server.

    python -m ctp500 text "# Hello" "- [ ] world"
    python -m ctp500 image photo.jpg --dither atkinson
    python -m ctp500 --preview out.png text "Check the layout first"
    python -m ctp500 serve --printer AA:BB:CC:DD:EE:FF
    python -m ctp500 add-printer m02pro           # find, pair and save a Phomemo M02 Pro
    python -m ctp500 --printer m02pro text "On the M02 Pro"
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

from . import __version__, config, printers, render


def _doc_from_args(args) -> dict:
    if args.command == "text":
        if args.file:
            with open(args.file, encoding="utf-8-sig") as f:
                text = f.read()
        elif not args.text or args.text == ["-"]:
            text = sys.stdin.read()
        else:
            text = "\n".join(args.text)
        if args.plain:
            return {"blocks": [{"type": "text", "text": text, "size": args.size, "font": args.font, "align": args.align}]}
        return {"blocks": [{"type": "markup", "markup": text, "size": args.size, "font": args.font}]}
    if args.command == "image":
        with open(args.path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        block = {"type": "image", "data": data, "name": os.path.basename(args.path), "dither": args.dither,
                 "width": args.width, "rotate": args.rotate, "brightness": args.brightness,
                 "contrast": args.contrast, "auto_contrast": args.auto_contrast,
                 "detail": args.detail, "weight": args.weight}
        blocks = [block]
        if args.caption:
            blocks.append({"type": "text", "text": args.caption, "align": "center", "size": 22})
        return {"blocks": blocks}
    if args.command == "qr":
        blocks = [{"type": "qr", "data": args.data, "size": args.size}]
        if args.caption:
            blocks.append({"type": "text", "text": args.caption, "align": "center", "size": 24, "bold": True})
        return {"blocks": blocks}
    if args.command == "banner":
        return {"blocks": [{"type": "banner", "text": " ".join(args.text), "font": args.font}]}
    if args.command == "doc":
        with open(args.path, encoding="utf-8-sig") as f:
            doc = json.load(f)
        doc = render.normalize(doc)
        base = os.path.dirname(os.path.abspath(args.path))
        for block in doc["blocks"]:  # pictures by path become embedded data, so a server can print them
            if isinstance(block, dict) and block.get("type") == "image" and block.get("path") and not block.get("data"):
                path = os.path.join(base, block.pop("path"))
                with open(path, "rb") as f:
                    block["data"] = base64.b64encode(f.read()).decode("ascii")
        return doc
    raise SystemExit(f"unknown command {args.command}")


def _http(method: str, url: str, token: str, body: dict | None = None, timeout: float = 150):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {"error": exc.reason}
        return exc.code, payload
    except urllib.error.URLError as exc:
        raise SystemExit(f"can't reach the print server at {url.split('/api')[0]}: {exc.reason}") from None


def _entry(args, cfg: dict) -> dict | None:
    """The printer --printer names (an id or name from the settings), the main one without it, or None when
    --printer is an address or 'sim' (a printer the settings don't know)."""
    entries = config.printer_entries(cfg)
    if not args.printer:
        return entries[0] if entries else None
    wanted = args.printer.strip().lower()
    return next((e for e in entries if wanted in (e["id"], e["label"].lower())), None)


def _is_address(value: str | None) -> bool:
    return bool(value) and (":" in value or value.lower() in ("sim", "simulated", "simulate"))


def _make(entry: dict, cfg: dict, keep_alive: float, args=None):
    from .printer import make_printer
    protocol = (args.protocol if args is not None and args.protocol else None) or entry["protocol"]
    return make_printer(entry["address"], cfg["data_dir"], entry["channel"], keep_alive, dialect=protocol,
                        model=entry["model"], printer_id=entry["id"], label=entry["label"], feed=entry["feed"],
                        paper=entry.get("paper"))


def _one_printer(args, cfg: dict, keep_alive: float = 0):
    """The printer to talk to straight over Bluetooth: --printer (a set-up printer, or an address with --model),
    else the main one. None when nothing is set up."""
    entry = _entry(args, cfg)
    if entry is None and _is_address(args.printer):
        model = printers.model(args.model)
        entry = {"id": model.key, "model": model.key, "address": args.printer, "label": model.short,
                 "channel": int(cfg.get("channel", 1)) if model is printers.CTP500 else model.channel,
                 "protocol": (cfg.get("protocol", "auto") if model is printers.CTP500 else "auto"),
                 "feed": int(cfg.get("feed", 2)) if model is printers.CTP500 else model.feed, "paper": None}
    if entry is None:
        if args.printer:
            raise SystemExit(f"error: no printer called {args.printer}; set up: "
                             f"{', '.join(e['id'] for e in config.printer_entries(cfg)) or 'none'}")
        return None
    return _make(entry, cfg, keep_alive, args)


def _paper(args, cfg: dict) -> printers.Paper:
    """The paper a --preview is drawn for."""
    entry = _entry(args, cfg)
    if entry is not None and not (_is_address(args.printer) and args.model):
        return printers.model(entry["model"]).paper_for(entry.get("paper"))
    return printers.model(args.model).paper


def _print_doc(doc: dict, args, cfg: dict) -> int:
    for key in ("darkness", "copies", "feed"):
        value = getattr(args, key, None)
        if value is not None:
            doc[key] = value
    if args.preview:
        paper = _paper(args, cfg)
        image = render.render(render.with_defaults(doc, cfg, paper), allow_files=True, paper=paper)
        image.save(args.preview)
        print(f"saved {args.preview} ({image.height} dots, about {paper.mm(image.height)} mm of paper)")
        return 0
    server = (args.server or cfg.get("server") or "").rstrip("/")
    if server and args.printer and not _is_address(args.printer):
        doc["printer"] = args.printer        # one of the server's printers
    if server and not _is_address(args.printer):
        code, payload = _http("POST", f"{server}/api/print?wait=1", args.token or cfg.get("token"), doc)
        job = payload.get("job") or {}
        if code == 200:
            print(f"printed ({job.get('length_mm', '?')} mm)")
            return 0
        print(f"error: {payload.get('error') or job.get('error') or job.get('status') or code}", file=sys.stderr)
        return 1
    printer = _one_printer(args, cfg)
    if printer is None:
        print("error: no printer set. Use --server http://your-pi:8500, --printer AA:BB:CC:DD:EE:FF,\n"
              "or save one: python -m ctp500 config server=http://your-pi:8500", file=sys.stderr)
        return 2
    from .transport import TransportError
    image = render.render(render.with_defaults(doc, cfg, printer.paper), allow_files=True, paper=printer.paper)
    try:
        printer.print_image(image, doc.get("darkness", cfg.get("darkness")),
                            int(doc.get("feed", printer.default_feed)), int(doc.get("copies", 1)))
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        printer.disconnect()
    print(f"printed ({printer.paper.mm(image.height)} mm)")
    return 0


def _add_printer(args, cfg: dict) -> int:
    """Find (Linux), pair and save another printer into the settings' "printers" list."""
    from . import discover
    model = printers.model(args.model)
    address = args.address
    if not address:
        print(f"looking for a {model.label} for {args.seconds} s (switch it on; it mustn't be connected to a phone)")
        try:
            found = [d for d in discover.scan(args.seconds) if d.get("model") == model.key]
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if len(found) != 1:
            print(f"found {len(found) or 'no'} {model.label}{'s' if len(found) > 1 else ''}"
                  + ("; give the address of yours:" if found else ". Is it on? You can also give its address."))
            for d in found:
                print(f"  python -m ctp500 add-printer {model.key} {d['address']}   # {d['name']}")
            return 1
        address = found[0]["address"]
        print(f"found {found[0]['name']} at {address}")
    if not _is_address(address):
        print(f"error: {address} isn't a Bluetooth address (AA:BB:CC:DD:EE:FF) or 'sim'", file=sys.stderr)
        return 2
    address = address.upper() if ":" in address else "sim"
    entry = {"model": model.key, "address": address}
    if args.paper:
        try:
            model.paper_for(args.paper)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        entry["paper"] = int(args.paper)
    if args.id:
        entry["id"] = args.id.strip().lower()
    if args.name:
        entry["name"] = args.name
    pid = entry.get("id") or model.key
    others = [e for e in cfg.get("printers") or []
              if str(e.get("id") or printers.model(e.get("model")).key).lower() != pid
              and str(e.get("address", "")).upper() != address]
    cfg["printers"] = others + [entry]
    if address != "sim" and sys.platform.startswith("linux"):
        print(f"pairing {address} (the printer must be on and not connected to a phone)")
        try:
            print(discover.pair(address))
        except RuntimeError as exc:
            print(f"pairing didn't finish ({exc}); the server will still try to connect")
    print(f"added {pid} ({entry.get('name') or model.label}, {address}); saved {config.save(cfg)}")
    print("Restart the server to use it (on the Pi: sudo systemctl restart ctp500).")
    return 0


def _add_print_options(p):
    p.add_argument("--darkness", help="light, normal, dark, darkest, or a number 0-255")
    p.add_argument("--copies", type=int)
    p.add_argument("--feed", type=int, help="~4 mm lines fed after the print to clear the tear bar (default 2)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ctp500", description="Print to a CTP500 or Phomemo M02 Pro Bluetooth "
                                                         "thermal printer.")
    ap.add_argument("--version", action="version", version=f"ctp500 {__version__}")
    ap.add_argument("--server", help="print through a ctp500 print server, e.g. http://raspberrypi.local:8500")
    ap.add_argument("--printer", help="which printer: one set up in the settings (e.g. m02pro), or a Bluetooth "
                                      "address / 'sim' to print straight to")
    ap.add_argument("--model", choices=tuple(printers.MODELS),
                    help="the model of a --printer address (default ctp500)")
    ap.add_argument("--protocol", choices=("auto", "cat", "escpos"), help="printer command dialect (default: auto)")
    ap.add_argument("--token", help="the server's access token, if it has one")
    ap.add_argument("--config", help="settings file (default: %(default)s)", default=config.default_path())
    ap.add_argument("--preview", metavar="PNG", help="save what would print to a PNG instead of printing")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("text", help="print a note (supports # headings, - [ ] checkboxes, ---, [qr] ...)")
    p.add_argument("text", nargs="*", help="lines to print; '-' or nothing reads standard input")
    p.add_argument("-f", "--file", help="read the note from a file")
    p.add_argument("--size", type=int, default=24)
    p.add_argument("--font", default="sans", help="sans, serif, mono, or a .ttf path")
    p.add_argument("--align", default="left", choices=("left", "center", "right"))
    p.add_argument("--plain", action="store_true", help="print the text exactly, no formatting")
    _add_print_options(p)

    p = sub.add_parser("image", help="print a picture")
    p.add_argument("path")
    p.add_argument("--style", "--dither", dest="dither", default="photo",
                   choices=("floyd", "atkinson", "threshold", "photo", "photoline", "line", "artist", "sketch", "coloring", "poster", "halftone",
                            "hatch"),
                   help="photo dithering (floyd, atkinson, threshold) or a drawing style")
    p.add_argument("--detail", type=float, default=0.5, help="drawing styles: 0 = only big shapes, 1 = fine detail")
    p.add_argument("--weight", type=int, default=2, choices=(1, 2, 3), help="drawing styles: line thickness")
    p.add_argument("--width", type=int, default=384, help="width in CTP500 dots (1/8 mm), up to 384")
    p.add_argument("--rotate", type=int, default=0, help="degrees clockwise, e.g. 90 for landscape pictures")
    p.add_argument("--brightness", type=float, default=1.0)
    p.add_argument("--contrast", type=float, default=1.0)
    p.add_argument("--auto-contrast", action="store_true")
    p.add_argument("--caption")
    _add_print_options(p)

    p = sub.add_parser("qr", help="print a QR code")
    p.add_argument("data")
    p.add_argument("--size", type=int, default=260)
    p.add_argument("--caption")
    _add_print_options(p)

    p = sub.add_parser("banner", help="huge sideways text along the paper")
    p.add_argument("text", nargs="+")
    p.add_argument("--font", default="sans")
    _add_print_options(p)

    p = sub.add_parser("doc", help="print a JSON layout (see README)")
    p.add_argument("path")
    _add_print_options(p)

    p = sub.add_parser("feed", help="feed paper")
    p.add_argument("lines", nargs="?", type=int, default=3)

    sub.add_parser("status", help="show the printer's state")

    p = sub.add_parser("find", help="look for the printer over Bluetooth")
    p.add_argument("--seconds", type=int, default=12)

    p = sub.add_parser("pair", help="pair and trust the printer (Linux)")
    p.add_argument("address")

    p = sub.add_parser("printers", help="list the printers that are set up")

    p = sub.add_parser("add-printer", help="set up another printer: add-printer m02pro [ADDRESS]")
    p.add_argument("model", choices=tuple(printers.MODELS))
    p.add_argument("address", nargs="?", help="its Bluetooth address, or 'sim' (default: look for it)")
    p.add_argument("--name", help="what the web app calls it")
    p.add_argument("--id", help="what prints call it (default: the model, e.g. m02pro)")
    p.add_argument("--seconds", type=int, default=15, help="how long to look for it")
    p.add_argument("--paper", help="the paper roll's width in mm (M02 Pro: 50, the default, or 53)")

    p = sub.add_parser("remove-printer", help="forget a printer added with add-printer")
    p.add_argument("id")

    p = sub.add_parser("serve", help="run the print server and web app")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--quiet", action="store_true", help="don't log every request")

    sub.add_parser("models", help="download the AI line-art models (checked by SHA-256)")

    p = sub.add_parser("config", help="show or change saved settings: config printer=AA:BB:.. port=8500")
    p.add_argument("pairs", nargs="*")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = config.load(args.config)
    except (OSError, ValueError) as exc:
        print(f"error: can't read {args.config}: {exc}", file=sys.stderr)
        return 2

    if args.command in ("text", "image", "qr", "banner", "doc"):
        try:
            return _print_doc(_doc_from_args(args), args, cfg)
        except render.RenderError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.command == "config":
        if not args.pairs:
            shown = {k: ("***" if k == "token" and v else v) for k, v in cfg.items() if not k.startswith("_")}
            print(json.dumps(shown, indent=2))
            print(f"(from {cfg['_path']})")
            return 0
        for pair in args.pairs:
            if "=" not in pair:
                print(f"error: expected key=value, got {pair}", file=sys.stderr)
                return 2
            key, value = pair.split("=", 1)
            if key not in config.DEFAULTS:
                print(f"error: unknown setting {key} (known: {', '.join(config.DEFAULTS)})", file=sys.stderr)
                return 2
            default = config.DEFAULTS[key]
            cfg[key] = int(value) if isinstance(default, int) and value.lstrip("-").isdigit() else value
        print(f"saved {config.save(cfg)}")
        return 0

    if args.command == "models":
        from . import ai
        for name in ai.MODELS:
            try:
                print(f"{name}: {ai.download(name)} ({ai.model_path(name)})")
            except (OSError, RuntimeError) as exc:
                print(f"error: {name}: {exc}", file=sys.stderr)
                return 1
        try:
            import onnxruntime
            print(f"onnxruntime {onnxruntime.__version__}: AI styles on")
        except ImportError:
            print("onnxruntime isn't installed (pip install onnxruntime): AI styles fall back to edge detection")
        return 0

    if args.command == "find":
        from . import discover
        try:
            print(f"looking for {args.seconds} s..." if sys.platform.startswith("linux") else "paired Bluetooth devices:")
            devices = discover.scan(args.seconds)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not devices:
            print("nothing found. Is the printer on (and not connected to a phone)?")
            return 1
        for d in devices:
            mark = f"  <- looks like a {printers.MODELS[d['model']].label}" if d.get("model") else ""
            print(f"{d['address']}  {d['name']}{' (paired)' if d['paired'] else ''}{mark}")
        return 0

    if args.command == "pair":
        from . import discover
        try:
            print(discover.pair(args.address))
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "printers":
        entries = config.printer_entries(cfg)
        if not entries:
            print("no printer set up yet (python -m ctp500 config printer=AA:BB:.., or add-printer)")
        for i, e in enumerate(entries):
            role = "main" if i == 0 and cfg.get("printer") else "    "
            model = printers.model(e["model"])
            mm = e.get("paper") or model.paper_mm(model.paper)
            paper = f"  {mm} mm paper" if model.papers else ""
            print(f"{role}  {e['id']:<10} {e['label']:<18} {e['address']}{paper}")
        return 0

    if args.command == "add-printer":
        return _add_printer(args, cfg)

    if args.command == "remove-printer":
        kept = [e for e in cfg.get("printers") or []
                if str(e.get("id") or printers.model(e.get("model")).key).lower() != args.id.lower()]
        if len(kept) == len(cfg.get("printers") or []):
            print(f"error: no added printer called {args.id} (the main printer is the 'printer' setting)",
                  file=sys.stderr)
            return 1
        cfg["printers"] = kept
        print(f"removed {args.id}; saved {config.save(cfg)}. Restart the server to use the change.")
        return 0

    server = (args.server or cfg.get("server") or "").rstrip("/")
    token = args.token or cfg.get("token")

    if args.command == "status":
        if server and not _is_address(args.printer):
            code, payload = _http("GET", f"{server}/api/status", token)
            print(json.dumps(payload, indent=2))
            return 0 if code == 200 else 1
        printer = _one_printer(args, cfg)
        if printer is None:
            print("error: no printer or server set", file=sys.stderr)
            return 2
        from .transport import TransportError
        try:
            print(json.dumps(printer.check(), indent=2))
        except TransportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            printer.disconnect()
        return 0

    if args.command == "feed":
        if server and not _is_address(args.printer):
            which = f"&printer={args.printer}" if args.printer and not _is_address(args.printer) else ""
            code, payload = _http("POST", f"{server}/api/feed?lines={args.lines}{which}", token, {})
            if code != 200:
                print(f"error: {payload.get('error', code)}", file=sys.stderr)
            return 0 if code == 200 else 1
        printer = _one_printer(args, cfg)
        if printer is None:
            print("error: no printer or server set", file=sys.stderr)
            return 2
        from .transport import TransportError
        try:
            printer.feed(args.lines)
        except TransportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            printer.disconnect()
        return 0

    if args.command == "serve":
        from .server import serve
        keep_alive = float(cfg.get("keep_alive", 45))
        try:
            if args.printer:            # just that one
                chain = [_one_printer(args, cfg, keep_alive)]
            else:                       # every printer in the settings, the main one first
                chain = [_make(e, cfg, keep_alive, args) for e in config.printer_entries(cfg)]
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not chain:
            print("error: no printer set. Use --printer AA:BB:CC:DD:EE:FF (find it with: python -m ctp500 find),\n"
                  "or --printer sim to try everything with a simulated printer.", file=sys.stderr)
            return 2
        if args.host:
            cfg["host"] = args.host
        if args.port:
            cfg["port"] = args.port
        if args.token:
            cfg["token"] = args.token
        httpd = serve(chain, cfg, quiet=args.quiet)
        host, port = httpd.server_address[:2]
        shown = "localhost" if host in ("0.0.0.0", "::") else host
        names = ", ".join(f"{p.id} ({p.name})" for p in chain)
        print(f"ctp500 {__version__}: {names}, web app at http://{shown}:{port}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            for p in chain:
                p.disconnect()
        return 0
    return 2
