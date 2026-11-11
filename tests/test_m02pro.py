"""The Phomemo M02 Pro and running more than one printer."""

import errno
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from unittest import mock

from PIL import Image

from ctp500 import cli, config, discover, phomemo, printers, raster, render, simulate, transport
from ctp500.printer import PrinterNotReady, make_printer
from ctp500.server import serve

M02 = printers.M02PRO_53           # the whole head (53 mm paper)
M02_50 = printers.M02PRO.paper     # the default: 50 mm paper, 536 dots a little left of the middle


class PhomemoProtocolTests(unittest.TestCase):
    def test_job_layout_and_round_trip(self):
        page = render.render("# Hello\n- [ ] world", paper=M02)
        self.assertEqual(page.width, 576)
        job = phomemo.build_job(raster.pack(page, 576), page.height, 72, "normal", feed=1)
        self.assertTrue(job.startswith(b"\x1b\x40\x1f\x11\x02\x02\x1f\x11\x37\x64\x1f\x11\x0b\x1f\x11\x35\x00"
                                       b"\x1d\x76\x30\x00\x48\x00"))
        self.assertTrue(job.endswith(b"\x1b\x4a\x2f\x1b\x64\x02\x1b\x64\x02"))
        paper, info = simulate.decode(job)
        self.assertEqual((info["dialect"], info["width"], info["density"], info["coefficient"]),
                         ("phomemo", 576, 2, 100))
        self.assertEqual(info["rows"], page.height + phomemo.TOP_ROWS)
        self.assertEqual(info["rasters"], -(-info["rows"] // phomemo.MAX_BLOCK_LINES))
        self.assertEqual((info["feed_dots"], info["feed_lines"]), (phomemo.LINE_DOTS, 4))
        top = phomemo.TOP_ROWS
        self.assertIsNone(paper.crop((0, 0, 576, top)).point(lambda v: 255 - v).getbbox())
        self.assertEqual(paper.crop((0, top, 576, top + page.height)).tobytes(), page.tobytes())

    def test_darkness(self):
        self.assertEqual(phomemo.darkness_setting(None), (2, 2, 100))
        self.assertEqual(phomemo.darkness_setting("light"), (1, 1, 100))
        self.assertEqual(phomemo.darkness_setting("darkest"), (4, 4, 150))
        self.assertEqual(phomemo.darkness_setting(3), (3, 4, 100))
        self.assertEqual(phomemo.darkness_setting("25"), (3, 4, 100))   # the CTP500's 0..255 scale

    def test_replies(self):
        data = b"\x00\x1a\x06\x88\x1a\x05\x98\x1a\x04\x50\x1a\x07\x01\x02\x03\x1a\x0f\x0c\x1a\x07\x01"
        self.assertEqual(phomemo.parse_replies(data)[:2], [(0x06, b"\x88"), (0x05, b"\x98")])
        state = phomemo.parse_state(data)
        self.assertEqual((state["problems"], state["battery"], state["firmware"], state["printed"]),
                         (["out_of_paper"], 80, "1.2.3", True))   # the cut-off last frame is ignored
        self.assertEqual(phomemo.faults(b"\x1a\x05\x99"), ["lid_open"])
        self.assertEqual(phomemo.faults(b"\x1a\x06\x89\x1a\x04\xa1"), [])
        self.assertEqual(phomemo.parse_state(b"\x1a\x04\xa2")["problems"], ["low_battery"])
        self.assertEqual([phomemo.parse_state(bytes([0x1A, 0x04, v]))["battery"] for v in (0x4B, 0x51, 0x5C, 0x64)],
                         [80, 80, 90, 100])                      # 75, 81, 92, 100: the jumpy reading, rounded


class M02ProPrinterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.printer = make_printer("sim", self.tmp.name, keep_alive=0, model="m02pro")

    def tearDown(self):
        self.tmp.cleanup()

    def test_prints_on_the_whole_head(self):
        self.assertEqual((self.printer.dialect, self.printer.id, self.printer.default_feed), ("phomemo", "m02pro", 0))
        self.assertEqual(self.printer.paper, M02_50)
        page = render.render("Hello", paper=self.printer.paper)
        self.assertEqual(page.width, 536)
        self.printer.print_image(page, "dark", 0)
        sim = self.printer.transport
        self.assertEqual((sim.last_info["dialect"], sim.last_info["width"], sim.last_info["density"]),
                         ("phomemo", 576, 4))
        paper, top = sim.last_paper, phomemo.TOP_ROWS
        self.assertEqual(paper.width, 576)
        # the print sits at dots 4..540 of the head, nothing outside it
        self.assertEqual(paper.crop((4, top, 540, top + page.height)).tobytes(), page.convert("1").tobytes())
        for blank in ((0, 0, 4, paper.height), (540, 0, 576, paper.height)):
            self.assertIsNone(paper.crop(blank).convert("L").point(lambda v: 255 - v).getbbox())
        status = self.printer.check()
        self.assertEqual((status["firmware"], status["battery"], status["problems"]), ("1.0.0", 80, []))
        snap = self.printer.snapshot()
        self.assertEqual((snap["dpi"], snap["width"], snap["paper_mm"]), (300, 536, 50))

    def test_paper_widths(self):
        wide = make_printer("sim", self.tmp.name, keep_alive=0, model="m02pro", paper=53)
        self.assertEqual((wide.paper.width, wide.paper.row_bytes, wide.snapshot()["paper_mm"]), (576, 72, 53))
        page = render.render("Hello", paper=wide.paper)
        wide.print_image(page, "normal", 0)
        top = phomemo.TOP_ROWS
        self.assertEqual(wide.transport.last_paper.crop((0, top, 576, top + page.height)).tobytes(),
                         page.convert("1").tobytes())
        self.assertEqual(printers.M02PRO.paper_for("50"), M02_50)
        with self.assertRaises(ValueError):
            make_printer("sim", self.tmp.name, model="m02pro", paper=40)
        with self.assertRaises(ValueError):
            make_printer("sim", self.tmp.name, paper=50)       # the CTP500 has one paper
        self.assertIsNone(make_printer("sim", self.tmp.name).snapshot()["paper_mm"])

    def test_out_of_paper_before_and_during_a_print(self):
        sim = self.printer.transport
        sim.state_flags = 1
        with self.assertRaises(PrinterNotReady) as caught:
            self.printer.print_image(render.render("x", paper=M02))
        self.assertEqual(str(caught.exception), "the printer says out of paper or the lid is open")
        sim.state_flags = 0
        sim.announce = b"\x1a\x05\x99"      # the lid opens halfway through
        tall = render.render({"blocks": [{"type": "space", "height": 600}]}, paper=M02)
        self.printer.connect()
        with self.assertRaises(PrinterNotReady) as caught:
            self.printer.print_image(tall)
        self.assertEqual(caught.exception.problems, ["lid_open"])
        self.assertFalse(sim.is_open)        # hung up, so a half-sent raster can't eat the next job

    def test_a_ctp500_protocol_is_refused(self):
        with self.assertRaises(ValueError):
            make_printer("sim", self.tmp.name, model="m02pro", dialect="cat")


class ScaledLayoutTests(unittest.TestCase):
    def test_same_size_on_paper(self):
        doc = {"blocks": [{"type": "heading", "text": "Title"}, {"type": "text", "text": "a line of text"},
                          {"type": "qr", "data": "https://example.com"}, {"type": "line"}]}
        small, big = render.render(doc), render.render(doc, paper=M02)
        self.assertEqual((small.width, big.width), (384, 576))
        self.assertAlmostEqual(big.height / small.height, 1.5, delta=0.04)
        banner = {"blocks": [{"type": "banner", "text": "HI THERE"}]}
        self.assertAlmostEqual(render.render(banner, paper=M02).height / render.render(banner).height, 1.5, delta=0.02)
        self.assertAlmostEqual(render._mm_to_dots(10, M02), 118)
        self.assertEqual(render._mm_to_dots(10), 80)
        # on 50 mm paper everything shrinks a little to fit 536 dots; millimetres stay millimetres
        narrow = render.render(doc, paper=M02_50)
        self.assertEqual(narrow.width, 536)
        self.assertAlmostEqual(narrow.height / small.height, 536 / 384, delta=0.05)
        self.assertAlmostEqual(render._mm_to_dots(10, M02_50), 118)

    def test_pictures(self):
        img = Image.new("RGB", (400, 300), "white")
        img.paste((0, 0, 0), (100, 80, 300, 220))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        import base64
        data = base64.b64encode(buf.getvalue()).decode()
        def ink_width(out):
            box = out.convert("L").point(lambda v: 255 - v).getbbox()
            return box[2] - box[0]
        for style in ("floyd", "threshold", "halftone"):
            doc = {"blocks": [{"type": "image", "data": data, "style": style, "width": 200}]}
            out = render.render(doc, paper=M02)
            self.assertEqual(out.width, 576)
            # as wide on paper as on the CTP500
            self.assertAlmostEqual(ink_width(out) / (1.5 * ink_width(render.render(doc))), 1.0, delta=0.04, msg=style)


class FineDotPhotoTests(unittest.TestCase):
    """Photos on the M02 Pro's 300 dpi come in clumps of dots: its lone dots print weakly (2026-11-11)."""

    @staticmethod
    def lone(img):
        import numpy as np
        a = np.asarray(img.convert("L")) < 128
        p = np.pad(a, 1)
        return float((a & ~(p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:])).sum() / max(1, a.sum()))

    def test_clumps_keep_the_tone(self):
        import numpy as np
        ink = lambda img: float((np.asarray(img.convert("L")) < 128).mean())   # noqa: E731
        mid, light = Image.new("L", (200, 200), 128), Image.new("L", (200, 200), 192)
        self.assertAlmostEqual(ink(raster.clustered_atkinson(mid)), 0.5, delta=0.02)
        clumped, plain = raster.clustered_atkinson(light), raster.to_1bit(light, "atkinson")
        self.assertGreater(self.lone(plain), 0.9)               # plain Atkinson: a light gray is all lone dots
        self.assertLess(self.lone(clumped), 0.02)
        self.assertTrue(0.08 < ink(clumped) < 0.2)               # still a light gray, not bare paper

    def test_only_finer_dots_get_them(self):
        import numpy as np
        from ctp500 import styles
        rng = np.random.default_rng(1)
        pic = Image.fromarray(np.clip(rng.normal(150, 40, (240, 320, 3)), 0, 255).astype(np.uint8), "RGB")
        self.assertGreater(self.lone(styles.thermal_photo(pic, 300, scale=1.0)), 0.03)   # the CTP500: as before
        self.assertLess(self.lone(styles.thermal_photo(pic, 300, scale=M02_50.scale)), 0.02)

    def test_photos_print_darker_on_finer_dots(self):
        cfg = {"darkness": "normal"}
        photo = lambda **kw: dict({"blocks": [{"type": "image", "data": "x", "style": "photoline"}]}, **kw)  # noqa: E731
        text = {"blocks": [{"type": "text", "text": "a sticker"}]}
        self.assertEqual(render.with_defaults(photo(), cfg, M02_50)["darkness"], "darkest")
        self.assertEqual(render.with_defaults(photo(darkness="auto"), cfg, M02)["darkness"], "darkest")
        self.assertEqual(render.with_defaults(photo(darkness="light"), cfg, M02_50)["darkness"], "light")  # picked
        self.assertEqual(render.with_defaults(dict(text), cfg, M02_50)["darkness"], "normal")             # text
        self.assertEqual(render.with_defaults(photo(), cfg, printers.CTP500.paper)["darkness"], "normal")
        self.assertEqual(render.with_defaults(photo(), cfg)["darkness"], "normal")
        self.assertEqual(render.with_defaults(photo(photo_look="even"), cfg, M02_50)["darkness"], "light")


class TwoPrinterServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"host": "127.0.0.1", "port": 0, "data_dir": self.tmp.name, "darkness": "normal", "feed": 2,
                    "history": 100, "token": ""}
        self.ctp = make_printer("sim", self.tmp.name, keep_alive=0)
        self.m02 = make_printer("sim", self.tmp.name, keep_alive=0, model="m02pro", label="Stickers")
        self.httpd = serve([self.ctp, self.m02], self.cfg, quiet=True)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                return res.status, res.read(), res.headers
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read(), exc.headers

    def test_status_lists_both(self):
        status = json.loads(self.call("/api/status")[1])
        self.assertEqual([p["id"] for p in status["printers"]], ["ctp500", "m02pro"])
        self.assertEqual((status["printer"]["id"], status["camera_printer"], status["width"]), ("ctp500", "ctp500", 384))
        self.assertEqual(status["printers"][1]["label"], "Stickers")

    def test_print_preview_and_reprint_per_printer(self):
        code, body, _ = self.call("/api/print?wait=1", {"text": "sticker", "printer": "m02pro"})
        job = json.loads(body)["job"]
        self.assertEqual((code, job["printer"], job["dpi"]), (200, "m02pro", 300))
        self.assertEqual(self.m02.transport.last_paper.width, 576)
        self.assertIsNone(self.ctp.transport.last_paper)
        code, body, _ = self.call("/api/print?wait=1&printer=stickers", {"text": "by name too"})
        self.assertEqual((code, json.loads(body)["job"]["printer"]), (200, "m02pro"))
        code, body, _ = self.call(f"/api/jobs/{job['id']}/reprint?wait=1", {})
        self.assertEqual(json.loads(body)["job"]["printer"], "m02pro")
        code, body, _ = self.call(f"/api/jobs/{job['id']}/reprint?wait=1&printer=ctp500", {})
        self.assertEqual((code, json.loads(body)["job"]["printer"]), (200, "ctp500"))
        self.assertEqual(self.ctp.transport.last_paper.width, 384)
        code, body, headers = self.call("/api/preview", {"text": "x", "printer": "m02pro"})
        self.assertEqual((code, headers["X-Printer"], Image.open(io.BytesIO(body)).width), (200, "m02pro", 536))
        code, body, _ = self.call("/api/preview", {"text": "x"})
        self.assertEqual(Image.open(io.BytesIO(body)).width, 384)
        code, body, _ = self.call("/api/print", {"text": "x", "printer": "nope"})
        self.assertEqual(code, 400)
        self.assertIn("ctp500, m02pro", json.loads(body)["error"])

    def test_camera_printer_setting(self):
        photo = io.BytesIO()
        Image.new("RGB", (64, 48), "gray").save(photo, "JPEG")
        code, body, _ = self.call("/api/settings", {"camera_printer": "elsewhere"})
        self.assertEqual(code, 400)
        code, body, _ = self.call("/api/settings", {"camera_printer": "m02pro"})
        self.assertEqual((code, json.loads(body)["camera_printer"]), (200, "m02pro"))
        svc = self.httpd.service
        name = svc.camera.save(photo.getvalue(), "IMG_20261104_120000.jpg")
        job = svc.camera_print(name, {"style": "floyd", "frame": False})
        self.assertTrue(job.finished.wait(20))
        self.assertEqual((job.status, job.printer), ("done", "m02pro"))
        job = svc.camera_print(name, {"style": "photo", "frame": False})
        self.assertTrue(job.finished.wait(30))
        self.assertEqual((job.status, job.darkness), ("done", "darkest"))      # photos on the M02 Pro: darkest
        job = svc.camera_print(name, {"style": "photo", "printer": "ctp500"})
        self.assertTrue(job.finished.wait(30))
        self.assertEqual((job.printer, job.darkness), ("ctp500", "normal"))
        png, height, dpi = svc.camera_preview(name, {"style": "floyd"})
        self.assertEqual((Image.open(io.BytesIO(png)).width, dpi), (536, 300))


class SetupTests(unittest.TestCase):
    def test_printer_entries(self):
        cfg = config.load(os.devnull + ".missing.json")
        cfg.update({"printer": "11:22:33:44:55:66", "feed": 3,
                    "printers": [{"model": "m02pro", "address": "AA:BB:CC:DD:EE:FF"},
                                 {"model": "M02 Pro", "address": "sim", "name": "Spare"}]})
        entries = config.printer_entries(cfg)
        self.assertEqual([e["id"] for e in entries], ["ctp500", "m02pro", "m02pro-2"])
        self.assertEqual([e["channel"] for e in entries], [1, 0, 0])
        self.assertEqual([e["feed"] for e in entries], [3, 0, 0])
        self.assertEqual(entries[2]["label"], "Spare")
        self.assertEqual([e["paper"] for e in entries], [None, None, None])
        self.assertEqual(config.DEFAULTS["printers"], [])           # load() copied the default list
        cfg["printers"][0]["paper"] = "53"
        self.assertEqual(config.printer_entries(cfg)[1]["paper"], 53)
        cfg["printers"][0]["paper"] = 40
        with self.assertRaises(ValueError):
            config.printer_entries(cfg)
        cfg["printers"][0]["paper"] = None
        cfg["printers"].append({"model": "laser", "address": "sim"})
        with self.assertRaises(ValueError):
            config.printer_entries(cfg)

    def test_add_and_remove_printer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w") as f:
                json.dump({"printer": "sim", "data_dir": tmp}, f)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["--config", path, "add-printer", "m02pro", "sim", "--name", "Stickers"]), 0)
                self.assertEqual(cli.main(["--config", path, "printers"]), 0)
                self.assertEqual(cli.main(["--config", path, "--printer", "m02pro", "--preview",
                                           os.path.join(tmp, "p.png"), "text", "hi"]), 0)
            with open(path) as f:
                saved = json.load(f)
            self.assertEqual(saved["printers"], [{"model": "m02pro", "address": "sim", "name": "Stickers"}])
            self.assertIn("m02pro     Stickers           sim  50 mm paper", out.getvalue())
            with Image.open(os.path.join(tmp, "p.png")) as preview:
                self.assertEqual(preview.width, 536)
            out = io.StringIO()
            with redirect_stdout(out):      # adding it again replaces it: here with 53 mm paper
                self.assertEqual(cli.main(["--config", path, "add-printer", "m02pro", "sim", "--paper", "53"]), 0)
                self.assertEqual(cli.main(["--config", path, "printers"]), 0)
                self.assertEqual(cli.main(["--config", path, "--printer", "m02pro", "--preview",
                                           os.path.join(tmp, "p.png"), "text", "hi"]), 0)
            self.assertIn("sim  53 mm paper", out.getvalue())
            with Image.open(os.path.join(tmp, "p.png")) as preview:
                self.assertEqual(preview.width, 576)
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(cli.main(["--config", path, "add-printer", "m02pro", "sim", "--paper", "40"]), 2)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--config", path, "remove-printer", "m02pro"]), 0)
            with open(path) as f:
                self.assertNotIn("printers", json.load(f))

    def test_pairing_output(self):
        text = ("Attempting to pair with AA:BB:CC:DD:EE:FF\n\x1b[0;93m[CHG]\x1b[0m Device AA:BB:CC:DD:EE:FF Paired: yes\n"
                "Pairing successful\n\x1b[0;91m[DEL]\x1b[0m Device 11:22:33:44:55:66 Sensor\n")
        self.assertEqual(discover._answers(text), ["Attempting to pair with AA:BB:CC:DD:EE:FF", "Pairing successful"])
        asked = []

        def run(args, timeout):
            asked.append(args[1] if args[1] != "--timeout" else args[3])
            return "\tPaired: yes\n" if args[1] == "info" else "Changing AA:BB:CC:DD:EE:FF trust succeeded\n"
        with mock.patch.object(discover, "_run", run), mock.patch.object(discover.sys, "platform", "linux"), \
                mock.patch.object(discover.shutil, "which", lambda name: "/usr/bin/" + name):
            self.assertEqual(discover.pair("AA:BB:CC:DD:EE:FF"),
                             "already paired\nChanging AA:BB:CC:DD:EE:FF trust succeeded")
        self.assertEqual(asked, ["info", "trust"])          # no second pairing

    def test_names(self):
        self.assertIs(printers.guess("M02 Pro"), printers.M02PRO)
        self.assertIs(printers.guess("Mini Printer-1025"), printers.CTP500)
        self.assertIsNone(printers.guess("Pixel 10"))


class AutoChannelTests(unittest.TestCase):
    def test_tries_channels_until_one_answers(self):
        tried = []

        class FakeSocket:
            def __init__(self, *args):
                pass

            def settimeout(self, t):
                pass

            def connect(self, addr):
                tried.append(addr[1])
                if addr[1] < 3:
                    raise OSError(errno.ECONNREFUSED if os.name != "nt" else 10061, "refused")

            def close(self):
                pass

        sock = transport.socket
        with mock.patch.object(sock, "AF_BLUETOOTH", 32, create=True), \
                mock.patch.object(sock, "BTPROTO_RFCOMM", 3, create=True), \
                mock.patch.object(sock, "socket", FakeSocket):
            link = transport.BluetoothTransport("AA:BB:CC:DD:EE:FF", 0)
            link.open()
            self.assertEqual((tried, link.channel), ([1, 2, 3], 3))
            link._sock = None
            fixed = transport.BluetoothTransport("AA:BB:CC:DD:EE:FF", 1)
            with self.assertRaises(transport.TransportError):
                fixed.open()                 # a set channel isn't second-guessed
            self.assertEqual(tried[-1], 1)


if __name__ == "__main__":
    unittest.main()
