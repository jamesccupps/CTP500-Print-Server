"""Command line: print, preview, find the printer, run the server.

    python -m ctp500 text "# Hello" "- [ ] world"
    python -m ctp500 image photo.jpg --dither atkinson
    python -m ctp500 --preview out.png text "Check the layout first"
    python -m ctp500 serve --printer AA:BB:CC:DD:EE:FF
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

from . import __version__, config, render


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


def _print_doc(doc: dict, args, cfg: dict) -> int:
    for key in ("darkness", "copies", "feed"):
        value = getattr(args, key, None)
        if value is not None:
            doc[key] = value
    if args.preview:
        image = render.render(render.with_defaults(doc, cfg), allow_files=True)
        image.save(args.preview)
        mm = round(image.height * 25.4 / 203)
        print(f"saved {args.preview} ({image.height} dots, about {mm} mm of paper)")
        return 0
    server = (args.server or cfg.get("server") or "").rstrip("/")
    if server:
        code, payload = _http("POST", f"{server}/api/print?wait=1", args.token or cfg.get("token"), doc)
        job = payload.get("job") or {}
        if code == 200:
            print(f"printed ({job.get('length_mm', '?')} mm)")
            return 0
        print(f"error: {payload.get('error') or job.get('error') or job.get('status') or code}", file=sys.stderr)
        return 1
    target = args.printer or cfg.get("printer")
    if not target:
        print("error: no printer set. Use --server http://your-pi:8500, --printer AA:BB:CC:DD:EE:FF,\n"
              "or save one: python -m ctp500 config server=http://your-pi:8500", file=sys.stderr)
        return 2
    from .printer import make_printer
    from .transport import TransportError
    printer = make_printer(target, cfg["data_dir"], int(cfg.get("channel", 1)), keep_alive=0,
                           dialect=args.protocol or cfg.get("protocol", "auto"))
    image = render.render(render.with_defaults(doc, cfg), allow_files=True)
    try:
        printer.print_image(image, doc.get("darkness", cfg.get("darkness")), int(doc.get("feed", cfg.get("feed", 2))),
                            int(doc.get("copies", 1)))
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        printer.disconnect()
    print(f"printed ({round(image.height * 25.4 / 203)} mm)")
    return 0


def _add_print_options(p):
    p.add_argument("--darkness", help="light, normal, dark, darkest, or a number 0-255")
    p.add_argument("--copies", type=int)
    p.add_argument("--feed", type=int, help="~4 mm lines fed after the print to clear the tear bar (default 2)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ctp500", description="Print to a CTP500 Bluetooth thermal printer.")
    ap.add_argument("--version", action="version", version=f"ctp500 {__version__}")
    ap.add_argument("--server", help="print through a ctp500 print server, e.g. http://raspberrypi.local:8500")
    ap.add_argument("--printer", help="print straight to this Bluetooth address (or 'sim')")
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
    p.add_argument("--width", type=int, default=384, help="dots wide, up to 384")
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
            mark = "  <- looks like a CTP500" if d["likely"] else ""
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

    server = (args.server or cfg.get("server") or "").rstrip("/")
    token = args.token or cfg.get("token")

    if args.command == "status":
        if server:
            code, payload = _http("GET", f"{server}/api/status", token)
            print(json.dumps(payload, indent=2))
            return 0 if code == 200 else 1
        target = args.printer or cfg.get("printer")
        if not target:
            print("error: no printer or server set", file=sys.stderr)
            return 2
        from .printer import make_printer
        from .transport import TransportError
        printer = make_printer(target, cfg["data_dir"], int(cfg.get("channel", 1)), keep_alive=0,
                               dialect=args.protocol or cfg.get("protocol", "auto"))
        try:
            print(json.dumps(printer.check(), indent=2))
        except TransportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            printer.disconnect()
        return 0

    if args.command == "feed":
        if server:
            code, payload = _http("POST", f"{server}/api/feed?lines={args.lines}", token, {})
            if code != 200:
                print(f"error: {payload.get('error', code)}", file=sys.stderr)
            return 0 if code == 200 else 1
        target = args.printer or cfg.get("printer")
        if not target:
            print("error: no printer or server set", file=sys.stderr)
            return 2
        from .printer import make_printer
        from .transport import TransportError
        printer = make_printer(target, cfg["data_dir"], int(cfg.get("channel", 1)), keep_alive=0,
                               dialect=args.protocol or cfg.get("protocol", "auto"))
        try:
            printer.feed(args.lines)
        except TransportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            printer.disconnect()
        return 0

    if args.command == "serve":
        from .printer import make_printer
        from .server import serve
        target = args.printer or cfg.get("printer")
        if not target:
            print("error: no printer set. Use --printer AA:BB:CC:DD:EE:FF (find it with: python -m ctp500 find),\n"
                  "or --printer sim to try everything with a simulated printer.", file=sys.stderr)
            return 2
        if args.host:
            cfg["host"] = args.host
        if args.port:
            cfg["port"] = args.port
        if args.token:
            cfg["token"] = args.token
        printer = make_printer(target, cfg["data_dir"], int(cfg.get("channel", 1)), float(cfg.get("keep_alive", 45)),
                               dialect=args.protocol or cfg.get("protocol", "auto"))
        httpd = serve(printer, cfg, quiet=args.quiet)
        host, port = httpd.server_address[:2]
        shown = "localhost" if host in ("0.0.0.0", "::") else host
        print(f"ctp500 {__version__}: {printer.name}, web app at http://{shown}:{port}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            printer.disconnect()
        return 0
    return 2
