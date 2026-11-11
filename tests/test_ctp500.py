import base64
import datetime as dt
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from PIL import Image, ImageDraw

from ctp500 import catproto, markup, protocol, raster, render, simulate
from ctp500.printer import make_printer
from ctp500.server import _title, serve


def png_b64(size=(200, 120)) -> str:
    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).ellipse([20, 10, 180, 110], fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


class ProtocolTests(unittest.TestCase):
    def test_pack_polarity_and_bit_order(self):
        img = Image.new("1", (protocol.WIDTH, 1), 1)
        img.putpixel((0, 0), 0)          # leftmost dot black
        img.putpixel((9, 0), 0)
        rows = raster.pack(img)
        self.assertEqual(len(rows), protocol.ROW_BYTES)
        self.assertEqual(rows[0], 0x80)  # MSB = leftmost, 1 = black
        self.assertEqual(rows[1], 0x40)
        self.assertTrue(all(b == 0 for b in rows[2:]))

    def test_unpack_is_inverse(self):
        img = render.render("# Hi\n- [ ] there")
        self.assertEqual(raster.unpack(raster.pack(img), img.height).tobytes(), img.tobytes())

    def test_job_round_trip_through_simulator(self):
        img = render.render({"blocks": [{"type": "text", "text": "x" * 40}, {"type": "space", "height": 600}]})
        job = protocol.build_job(raster.pack(img), img.height, "dark", feed=2)
        self.assertTrue(job.startswith(protocol.INIT + b"\x1d\x49\xf0\x19"))
        paper, info = simulate.decode(job)
        self.assertEqual(info["darkness"], [25])
        self.assertEqual(info["feeds"], 2)
        self.assertEqual(info["rasters"], -(-img.height // protocol.MAX_BLOCK_LINES))
        self.assertEqual(paper.crop((0, 0, protocol.WIDTH, img.height)).tobytes(), img.tobytes())

    def test_darkness_values(self):
        self.assertEqual(protocol.darkness_value(None), 15)
        self.assertEqual(protocol.darkness_value("Light"), 8)
        self.assertEqual(protocol.darkness_value("30"), 30)
        self.assertEqual(protocol.darkness_value(999), 255)

    def test_simulator_rejects_garbage(self):
        with self.assertRaises(simulate.DecodeError):
            simulate.decode(b"\x1b\x40\xff")


class CatProtocolTests(unittest.TestCase):
    def test_crc_and_packet_match_the_real_printer(self):
        self.assertEqual(catproto.crc8(b"123456789"), 0xF4)
        self.assertEqual(catproto.state_request().hex(" "), "51 78 a3 00 01 00 00 00 ff")
        # a reply captured from the Mini Printer-1025
        reply = bytes.fromhex("51 78 a3 01 03 00 00 0b 28 4f ff 51 78 a8 01 0a 00 ba 00 03 33 2e 30 2e 35 30 48 7c ff")
        state = catproto.parse_state(reply)
        self.assertEqual((state["state_flags"], state["firmware"]), (0, "3.0.50H"))

    def test_round_trip_raw_and_compressed_rows(self):
        img = render.render({"blocks": [
            {"type": "heading", "text": "Rows"},                       # simple rows: compressed
            {"type": "image", "data": png_b64((300, 300)), "dither": "floyd"},
        ]})
        noisy = Image.effect_noise((protocol.WIDTH, 40), 120).convert("1")  # busy rows: raw
        page = Image.new("1", (protocol.WIDTH, img.height + 40), 1)
        page.paste(img, (0, 0))
        page.paste(noisy, (0, img.height))
        job = catproto.build_job(raster.pack(page), page.height, "dark", feed_dots=96)
        paper, info = simulate.decode(job)
        self.assertEqual((info["level"], info["energy"], info["feed_dots"]), (4, 19000, 0))
        self.assertEqual((paper.height, info["rows"]), (page.height + 96, page.height + 96))  # feed = blank rows
        self.assertIsNone(paper.crop((0, page.height, protocol.WIDTH, paper.height)).point(lambda v: 255 - v).getbbox())
        self.assertGreater(info["rle_rows"], 0)
        self.assertLess(info["rle_rows"], info["rows"])
        self.assertEqual(paper.crop((0, 0, protocol.WIDTH, page.height)).tobytes(), page.tobytes())

    def test_parse_skips_garbage_and_bad_crc(self):
        good = catproto.packet(0xAE, b"\x10")
        bad = bytearray(catproto.packet(0xAE, b"\x00"))
        bad[-2] ^= 1
        self.assertEqual(catproto.parse_packets(b"\x00junk" + bytes(bad) + good), [(0xAE, b"\x10")])
        self.assertTrue(catproto.flow_signal(good))
        self.assertFalse(catproto.flow_signal(catproto.packet(0xAE, b"\x00")))
        self.assertIsNone(catproto.flow_signal(b"nothing"))

    def test_darkness_settings(self):
        self.assertEqual(catproto.darkness_setting(None), (3, 14000))
        self.assertEqual(catproto.darkness_setting("light"), (2, 10000))
        self.assertEqual(catproto.darkness_setting(5), (5, 26000))
        self.assertEqual(catproto.darkness_setting("25"), (4, 19000))

    def test_auto_detect_on_the_simulator(self):
        with tempfile.TemporaryDirectory() as tmp:
            for wanted, found in (("auto", "cat"), ("escpos", "escpos"), ("cat", "cat")):
                printer = make_printer("sim", tmp, keep_alive=0, dialect=wanted)
                printer.print_image(render.render("hello"))
                self.assertEqual(printer.dialect, found)
                self.assertEqual(printer.transport.last_info["dialect"], found)


class MarkupTests(unittest.TestCase):
    def test_kinds(self):
        blocks = markup.parse("# Title\n- [ ] a\n- [x] b\n- [ ]\n- c\n---\n- - -\n===\nMilk ..... $3\n> Note\n"
                              "[qr] https://x.y\n[banner] HI\n[center][big] Big\n**Bold**\nplain\nmore")
        kinds = [b["type"] for b in blocks]
        self.assertEqual(kinds, ["heading", "checklist", "list", "line", "line", "line", "row", "text", "qr",
                                 "banner", "text", "text", "text"])
        self.assertEqual(blocks[1]["items"], [{"text": "a", "done": False}, {"text": "b", "done": True},
                                              {"text": "", "done": False}])
        self.assertEqual(blocks[6]["left"], "Milk")
        self.assertEqual(blocks[6]["right"], "$3")
        self.assertTrue(blocks[7]["invert"])
        self.assertEqual((blocks[10]["align"], blocks[10]["size"]), ("center", 32))
        self.assertTrue(blocks[11]["bold"])
        self.assertEqual(blocks[12]["text"], "plain\nmore")

    def test_blank_lines_become_space_but_not_at_the_ends(self):
        blocks = markup.parse("\n\na\n\nb\n\n")
        self.assertEqual([b["type"] for b in blocks], ["text", "space", "text"])

    def test_placeholders(self):
        when = dt.datetime(2026, 10, 30, 15, 4)
        self.assertEqual(render.expand("{weekday} {date} {time}", when), "Friday Oct 30, 2026 3:04 PM")
        self.assertEqual(render.expand("{nope}", when), "{nope}")


class RenderTests(unittest.TestCase):
    def test_every_block_type_is_full_width_1bit(self):
        doc = {"blocks": [
            {"type": "heading", "text": "Hello"},
            {"type": "text", "text": "word " * 60, "align": "center", "bold": True, "invert": True},
            {"type": "image", "data": png_b64(), "dither": "atkinson", "width": 200},
            {"type": "image", "data": "data:image/png;base64," + png_b64(), "rotate": 90, "dither": "threshold"},
            {"type": "qr", "data": "https://example.com", "caption": "scan"},
            {"type": "line", "style": "dotted"},
            {"type": "space", "height": 10},
            {"type": "row", "left": "Long item name " * 4, "right": "$9.99", "leader": True},
            {"type": "list", "items": ["one", "two"]},
            {"type": "checklist", "items": [{"text": "done", "done": True}, "todo"]},
            {"type": "banner", "text": "WOW"},
            {"type": "markup", "markup": "## inside\n- [ ] item"},
        ]}
        img = render.render(doc)
        self.assertEqual((img.mode, img.width), ("1", protocol.WIDTH))
        self.assertGreater(img.height, 1000)

    def test_drawing_styles(self):
        from ctp500 import styles
        img = Image.new("L", (300, 200), 230)
        ImageDraw.Draw(img).ellipse([60, 30, 240, 170], fill=60)       # a dark disc on a light ground
        data = io.BytesIO()
        img.save(data, "PNG")
        b64 = base64.b64encode(data.getvalue()).decode()
        for style in styles.STYLES:
            for weight in (1, 3):
                out = render.render({"blocks": [{"type": "image", "data": b64, "style": style, "weight": weight,
                                                 "detail": 0.3}]})
                self.assertEqual((out.mode, out.width), ("1", protocol.WIDTH), style)
                ink = 1 - (sum(out.convert("L").histogram()[:128]) / (out.width * out.height))
                self.assertLess(ink, 1.0, style)
        line = render.render({"blocks": [{"type": "image", "data": b64, "style": "line"}]}).convert("L")
        w, h = line.size
        center = line.crop((w // 2 - 20, h // 2 - 20, w // 2 + 20, h // 2 + 20))
        self.assertIsNone(center.point(lambda v: 255 - v).getbbox())   # inside the disc stays paper
        self.assertIsNotNone(line.point(lambda v: 255 - v).getbbox())   # but there is an outline
        with self.assertRaisesRegex(render.RenderError, "unknown style 'crayon'"):
            render.render({"blocks": [{"type": "image", "data": b64, "style": "crayon"}]})

    def test_ai_styles_fall_back_without_the_models(self):
        from ctp500 import ai, styles
        b64 = png_b64((320, 240))
        saved = os.environ.get("CTP500_MODELS")
        with tempfile.TemporaryDirectory() as empty:
            os.environ["CTP500_MODELS"] = empty
            try:
                self.assertFalse(ai.available("lineart"))
                self.assertFalse(styles.uses_ai("line"))
                for style in ("line", "artist", "coloring"):
                    out = render.render({"blocks": [{"type": "image", "data": b64, "style": style}]})
                    self.assertEqual(out.width, protocol.WIDTH, style)
            finally:
                if saved is None:
                    os.environ.pop("CTP500_MODELS", None)
                else:
                    os.environ["CTP500_MODELS"] = saved

    @unittest.skipUnless(__import__("ctp500.ai", fromlist=["ai"]).available("lineart"), "AI models not installed")
    def test_ai_line_art(self):
        from ctp500 import ai, styles
        img = Image.new("RGB", (400, 300), "white")
        ImageDraw.Draw(img).ellipse([100, 50, 300, 250], outline="black", width=6)
        drawing = ai.draw(img, "lineart", 400)
        self.assertEqual((drawing.mode, drawing.size), ("L", (400, 300)))
        for weight in (1, 2, 3):
            bw = styles.apply_ai(img, "line", 384, 0.5, weight)
            self.assertEqual((bw.mode, bw.width), ("1", 384))
            self.assertIsNotNone(bw.convert("L").point(lambda v: 255 - v).getbbox())   # it drew the circle

    def test_shorthands(self):
        for doc in ("hello", {"text": "hello"}, {"markup": "# hello"}, [{"type": "text", "text": "hello"}]):
            self.assertEqual(render.render(doc).width, protocol.WIDTH)

    def test_errors_name_the_block(self):
        with self.assertRaisesRegex(render.RenderError, "block 2: unknown type"):
            render.render({"blocks": [{"type": "text", "text": "ok"}, {"type": "sparkles"}]})
        with self.assertRaisesRegex(render.RenderError, "only works from the command line"):
            render.render({"blocks": [{"type": "image", "path": "secret.png"}]})
        with self.assertRaisesRegex(render.RenderError, "couldn't read the image"):
            render.render({"blocks": [{"type": "image", "data": base64.b64encode(b"nope").decode()}]})
        with self.assertRaises(render.RenderError):
            render.render({"blocks": [{"type": "space", "height": 2000}] * 12})

    def test_padding_adds_blank_paper(self):
        plain = render.render("hello")
        padded = render.render({"markup": "hello", "padding_top_mm": 6, "padding_bottom_mm": 4})
        self.assertEqual(padded.height, plain.height + 48 + 32)
        self.assertIsNone(padded.crop((0, 0, protocol.WIDTH, 48)).point(lambda v: 255 - v).getbbox())
        self.assertEqual(padded.crop((0, 48, protocol.WIDTH, 48 + plain.height)).tobytes(), plain.tobytes())
        doc = render.with_padding({"blocks": [], "padding_top_mm": 0}, {"padding_top_mm": 6, "padding_bottom_mm": 6})
        self.assertEqual((doc["padding_top_mm"], doc["padding_bottom_mm"]), (0, 6))  # the document wins

    def test_wrap_breaks_long_words(self):
        from ctp500 import fonts
        font, _ = fonts.load("sans", 24)
        lines = render.wrap("x" * 300, font, 300)
        self.assertGreater(len(lines), 2)
        self.assertTrue(all(font.getlength(line) <= 300 for line in lines))

    def test_titles_hide_wifi_passwords(self):
        self.assertEqual(_title({"blocks": [{"type": "qr", "data": "WIFI:T:WPA;S:Home;P:secret;;"}]}), "Wi-Fi QR (Home)")
        self.assertEqual(_title({"blocks": [{"type": "qr", "data": "WIFI:S:x;P:y;;"},
                                            {"type": "text", "text": "Guest Wi-Fi"}]}), "Guest Wi-Fi")
        self.assertEqual(_title({"blocks": [{"type": "line"}, {"type": "checklist", "items": ["eggs"]}]}), "eggs")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"host": "127.0.0.1", "port": 0, "data_dir": self.tmp.name, "darkness": "normal", "feed": 4,
                    "history": 100, "token": ""}
        self.start()

    def start(self):
        printer = make_printer("sim", self.tmp.name, keep_alive=0)
        self.httpd = serve(printer, self.cfg, quiet=True)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def tearDown(self):
        self.stop()
        self.tmp.cleanup()

    def call(self, path, body=None, method=None, headers=None):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data is not None else "GET"))
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if data is not None and not isinstance(body, bytes):
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                return res.status, res.read(), res.headers
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read(), exc.headers

    def test_print_text_waits_and_lands_on_paper(self):
        code, body, _ = self.call("/api/print/text?wait=1", b"# Hello\n- [ ] world")
        job = json.loads(body)["job"]
        self.assertEqual((code, job["status"]), (200, "done"))
        self.assertEqual(job["title"], "Hello")
        paper = os.listdir(os.path.join(self.tmp.name, "simulated-paper"))
        self.assertEqual(len(paper), 1)
        code, body, headers = self.call(job["preview"])
        self.assertEqual((code, headers["Content-Type"]), (200, "image/png"))

    def test_print_json_image_and_reprint(self):
        doc = {"blocks": [{"type": "image", "data": png_b64()}], "copies": 2, "darkness": "dark"}
        code, body, _ = self.call("/api/print?wait=1", doc)
        job = json.loads(body)["job"]
        self.assertEqual((code, job["copies"], job["darkness"]), (200, 2, "dark"))
        code, body, _ = self.call(f"/api/jobs/{job['id']}/reprint?wait=1", {})
        self.assertEqual((code, json.loads(body)["job"]["status"]), (200, "done"))
        code, body, _ = self.call("/api/jobs")
        self.assertEqual(len(json.loads(body)["jobs"]), 2)

    def test_raw_image_endpoint(self):
        raw = base64.b64decode(png_b64())
        code, body, _ = self.call("/api/print/image?wait=1&dither=threshold&caption=hi", raw,
                                  headers={"Content-Type": "image/png"})
        self.assertEqual(code, 200, body)
        code, body, _ = self.call("/api/print/image?wait=1&style=line&detail=0.7&weight=1", raw,
                                  headers={"Content-Type": "image/jpeg"})
        self.assertEqual(code, 200, body)

    def test_preview_and_status(self):
        code, body, headers = self.call("/api/preview", {"markup": "# Preview"})
        self.assertEqual(code, 200)
        self.assertEqual(Image.open(io.BytesIO(body)).width, protocol.WIDTH)
        self.assertGreater(int(headers["X-Print-Height"]), 10)
        code, body, _ = self.call("/api/status")
        status = json.loads(body)
        self.assertTrue(status["printer"]["simulated"])
        self.assertEqual(status["width"], 384)

    def test_settings_padding_reaches_prints_and_previews(self):
        self.stop()
        self.cfg.update({"padding_top_mm": 5, "padding_bottom_mm": 5})
        self.start()
        plain = render.render("pad me")
        _, body, headers = self.call("/api/preview", {"markup": "pad me"})
        self.assertEqual(int(headers["X-Print-Height"]), plain.height + 80)
        job = json.loads(self.call("/api/print/text?wait=1", b"pad me")[1])["job"]
        self.assertEqual(job["height"], plain.height + 80)

    def test_settings_api_saves_and_validates(self):
        self.stop()
        path = os.path.join(self.tmp.name, "config.json")
        self.cfg.update({"padding_top_mm": 6, "padding_bottom_mm": 6, "_path": path})
        self.start()
        pads = lambda d: {k: d[k] for k in ("padding_top_mm", "padding_bottom_mm")}
        self.assertEqual(pads(json.loads(self.call("/api/settings")[1])), {"padding_top_mm": 6, "padding_bottom_mm": 6})
        code, body, _ = self.call("/api/settings", {"padding_top_mm": 3.5, "padding_bottom_mm": 10})
        self.assertEqual((code, pads(json.loads(body))), (200, {"padding_top_mm": 3.5, "padding_bottom_mm": 10}))
        with open(path) as f:
            self.assertEqual(json.load(f)["padding_bottom_mm"], 10)
        plain = render.render("x")
        _, _, headers = self.call("/api/preview", {"markup": "x"})
        self.assertEqual(int(headers["X-Print-Height"]), plain.height + 28 + 80)
        for bad in ({"padding_top_mm": -1}, {"padding_top_mm": "lots"}, {"printer": "AA"}, [1]):
            self.assertEqual(self.call("/api/settings", bad)[0], 400, bad)
        self.assertEqual(json.loads(self.call("/api/settings")[1])["padding_top_mm"], 3.5)

    def _jpeg(self, color="gray"):
        img = Image.new("RGB", (640, 480), "white")
        ImageDraw.Draw(img).ellipse([160, 80, 480, 400], fill=color)
        buf = io.BytesIO()
        img.save(buf, "JPEG")
        return buf.getvalue()

    def test_camera_upload_gallery_and_print(self):
        photo = self._jpeg()
        headers = {"Content-Type": "image/jpeg", "X-Filename": "IMG00007.jpg"}
        code, body, _ = self.call("/api/camera/upload", photo, headers=headers)
        self.assertEqual((code, json.loads(body)["name"]), (200, "IMG00007.jpg"))
        self.call("/api/camera/upload", photo, headers=headers)            # a re-sync keeps one copy
        other = self._jpeg("black")
        self.assertEqual(json.loads(self.call("/api/camera/upload", other, headers=headers)[1])["name"],
                         "IMG00007-2.jpg")                                  # a different photo, same name
        photos = json.loads(self.call("/api/camera/photos")[1])["photos"]
        self.assertEqual(sorted(p["name"] for p in photos), ["IMG00007-2.jpg", "IMG00007.jpg"])
        code, body, headers_ = self.call(photos[0]["thumb"])
        self.assertEqual((code, headers_["Content-Type"]), (200, "image/jpeg"))
        self.assertLessEqual(Image.open(io.BytesIO(body)).width, 240)

        # the camera's instant print: answers 200 at once, and the job prints framed, in the camera style
        code, body, _ = self.call("/api/camera/print", self._jpeg("red"),
                                  headers={"Content-Type": "image/jpeg", "X-Filename": "IMG00008.jpg"})
        reply = json.loads(body)
        self.assertEqual((code, reply["name"]), (200, "IMG00008.jpg"))
        job = json.loads(self.call(f"/api/jobs/{reply['job']['id']}")[1])
        for _ in range(100):
            if job["status"] in ("done", "failed"):
                break
            threading.Event().wait(0.05)
            job = json.loads(self.call(f"/api/jobs/{reply['job']['id']}")[1])
        self.assertEqual((job["status"], job["source"]), ("done", "camera"))

        code, body, _ = self.call("/api/camera/photo/IMG00007.jpg/print?wait=1&style=sketch", {})
        self.assertEqual((code, json.loads(body)["job"]["status"]), (200, "done"))
        self.assertEqual(self.call("/api/camera/photo/IMG00007.jpg/delete", {})[0], 200)
        self.assertEqual(self.call("/api/camera/photo/IMG00007.jpg")[0], 404)

    def test_camera_downloads(self):
        import zipfile
        photos = {"IMG00001.jpg": self._jpeg("gray"), "IMG00002.jpg": self._jpeg("black")}
        for name, data in photos.items():
            self.call("/api/camera/upload", data, headers={"Content-Type": "image/jpeg", "X-Filename": name})
        code, body, headers = self.call("/api/camera/photo/IMG00001.jpg?download=1")
        self.assertEqual((code, body), (200, photos["IMG00001.jpg"]))          # the original, untouched
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="IMG00001.jpg"')
        code, body, headers = self.call("/api/camera/photos.zip")
        self.assertEqual((code, headers["Content-Type"]), (200, "application/zip"))
        self.assertIn("attachment", headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            self.assertEqual({n: z.read(n) for n in z.namelist()}, photos)
        def leftovers():
            return [f for f in os.listdir(os.path.join(self.tmp.name, "camera")) if f.endswith(".zip")]
        # the server removes the temp zip just after the last byte goes out, so it may still be there for a moment
        # (a slow Windows runner looked too soon once, 2026-11-11)
        deadline = time.monotonic() + 5
        while leftovers() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(leftovers(), [])                                        # the temp zip is gone

    def test_photo_browser_support(self):
        png = io.BytesIO()
        Image.new("RGB", (400, 300), "navy").save(png, "PNG")
        code, body, _ = self.call("/api/camera/upload", png.getvalue(),
                                  headers={"Content-Type": "image/png", "X-Filename": "My Photo (1).png"})
        name = json.loads(body)["name"]
        self.assertEqual((code, name), (200, "My_Photo_1.jpg"))
        self.assertEqual(Image.open(io.BytesIO(self.call(f"/api/camera/photo/{name}")[1])).format, "JPEG")
        self.call("/api/camera/upload", self._jpeg(), headers={"X-Filename": "IMG_20260614_144700.jpg"})
        photos = {p["name"]: p for p in json.loads(self.call("/api/camera/photos")[1])["photos"]}
        taken = time.localtime(photos["IMG_20260614_144700.jpg"]["taken"])
        self.assertEqual((taken.tm_year, taken.tm_mon, taken.tm_mday, taken.tm_hour), (2026, 6, 14, 14))
        self.assertEqual(list(photos)[-1], "IMG_20260614_144700.jpg")            # oldest taken = last
        code, body, headers = self.call(f"/api/camera/photo/{name}/preview", {"style": "photo"})
        self.assertEqual((code, headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(Image.open(io.BytesIO(body)).width, protocol.WIDTH)
        self.assertGreater(int(headers["X-Print-Length-Mm"]), 20)
        self.assertEqual(len(json.loads(self.call("/api/jobs")[1])["jobs"]), 0)  # a preview prints nothing
        code, body, headers = self.call("/photos")
        self.assertEqual(code, 200)
        self.assertIn(b"photos.js", body)

    def test_lithophane_preview_and_stl(self):
        name = json.loads(self.call("/api/camera/upload", self._jpeg(),
                                    headers={"X-Filename": "IMG00010.jpg"})[1])["name"]
        code, body, headers = self.call(f"/api/camera/litho/{name}.png?size=60")
        self.assertEqual((code, headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(headers["X-Litho-Mm"], "60 x 45")
        code, body, headers = self.call(f"/api/camera/litho/{name}.stl?size=60&shape=curved")
        self.assertEqual(code, 200)
        self.assertIn('filename="IMG00010-lithophane-curved-60mm.stl"', headers["Content-Disposition"])
        count = int.from_bytes(body[80:84], "little")
        self.assertEqual(len(body), 84 + 50 * count)
        self.assertEqual(self.call(f"/api/camera/litho/{name}.stl?size=500")[0], 400)
        self.assertEqual(self.call(f"/api/camera/litho/{name}.stl?shape=cube")[0], 400)
        from unittest import mock
        from ctp500 import ai
        with mock.patch.object(ai, "available", lambda name="lineart": name != "depth"):   # models not fetched
            code, body, _ = self.call(f"/api/camera/litho/{name}.png?shape=relief")
        self.assertEqual(code, 503)
        self.assertIn("python -m ctp500 models", json.loads(body)["error"])
        self.assertEqual(self.call(f"/api/camera/litho/{name}.obj")[0], 404)
        self.assertEqual(self.call("/api/camera/litho/IMG99999.jpg.png")[0], 404)

    def _fake_helper(self, kinds, answer, stop):
        """A stand-in for the PC helper (desktop/worker.py): long-polls and answers with answer(kind, data, params)."""
        def run():
            while not stop.is_set():
                try:
                    code, body, headers = self.call(f"/api/helper/next?kinds={kinds}&name=test&wait=1")
                except OSError:
                    return                                              # the test's server has stopped
                if code != 200:
                    continue
                out, meta = answer(headers["X-Job-Kind"], body, json.loads(headers["X-Job-Params"]))
                self.call(f"/api/helper/result/{headers['X-Job-Id']}", out, headers={"X-Job-Meta": json.dumps(meta)})
        threading.Thread(target=run, daemon=True).start()
        for _ in range(50):                                            # until the Pi has seen it
            if json.loads(self.call("/api/status")[1])["helper"]["online"]:
                return
            time.sleep(0.05)

    def test_pc_helper_slices_and_draws(self):
        name = json.loads(self.call("/api/camera/upload", self._jpeg(),
                                    headers={"X-Filename": "IMG00012.jpg"})[1])["name"]
        self.assertFalse(json.loads(self.call("/api/status")[1])["helper"]["online"])
        code, body, _ = self.call(f"/api/camera/litho/{name}.3mf?size=60")      # no PC: say so
        self.assertEqual(code, 503)
        self.assertIn("turn the PC on", json.loads(body)["error"])
        self.assertEqual(self.call(f"/api/camera/litho/{name}.png?shape=figurine")[0], 400)   # not a shape (any more)
        seen = {}

        def answer(kind, data, params):
            seen[kind] = params
            if kind == "slice":                                         # the Pi's own STL (binary, counted)
                self.assertEqual(len(data), 84 + 50 * int.from_bytes(data[80:84], "little"))
                return b"PK-sliced", {"minutes": 18}
            return data, {}                                             # draw: hand the picture back (RGB)
        from ctp500 import camera, render
        self.assertIsNone(render.SHARPEN(Image.new("RGB", (64, 48))))            # PC off: photos as taken
        self.assertTrue(camera.print_doc(self._jpeg(), "x.jpg", {}, {"sharpen": True})["blocks"][0]["sharpen_faces"])
        self.assertNotIn("sharpen_faces", camera.print_doc(self._jpeg(), "x.jpg", {})["blocks"][0])
        stop = threading.Event()
        self.addCleanup(stop.set)
        self._fake_helper("slice,draw,restore", answer, stop)
        status = json.loads(self.call("/api/status")[1])["helper"]
        self.assertEqual((status["online"], status["name"], status["kinds"]),
                         (True, "test", ["draw", "restore", "slice"]))
        code, body, _ = self.call(f"/api/camera/litho/{name}.3mf?shape=flat&size=80")   # (relief needs the models)
        self.assertEqual((code, body, seen["slice"]["shape"]), (200, b"PK-sliced", "flat"))
        sharp = render.SHARPEN(Image.new("RGB", (64, 48), (9, 99, 199)))         # the PC's picture comes back
        self.assertEqual((sharp.size, seen["restore"]), ((64, 48), {"model": "gfpgan_1.4"}))
        code, body, headers = self.call(f"/api/camera/litho/{name}.3mf?size=60&shape=curved")
        self.assertEqual((code, body), (200, b"PK-sliced"))
        self.assertIn('IMG00012-lithophane-curved-60mm.gcode.3mf"', headers["Content-Disposition"])
        self.assertEqual(seen["slice"], {"name": "IMG00012", "size": 60.0, "shape": "curved"})
        from ctp500 import ai
        picture = Image.new("RGB", (96, 64), (200, 10, 10))
        drawing = ai.REMOTE("lineart", picture)                         # the AI drawing goes to the PC
        self.assertEqual((drawing.mode, drawing.size), ("L", (96, 64)))
        self.assertEqual(seen["draw"], {"model": "lineart"})


    def test_faster_previews_and_defaults(self):
        from ctp500 import ai
        # pictures without a style get the thermal-tuned photo print
        self.assertEqual(render.DEFAULT_PICTURE_STYLE, "photo")
        b64 = base64.b64encode(self._jpeg()).decode()
        plain = render.render({"blocks": [{"type": "image", "data": b64}]})
        tuned = render.render({"blocks": [{"type": "image", "data": b64, "style": "photo"}]})
        self.assertEqual(plain.tobytes(), tuned.tobytes())
        # a 12 MP phone-sized JPEG decodes reduced and still fills the paper
        big = io.BytesIO()
        Image.new("RGB", (4000, 3000), "gray").save(big, "JPEG")
        out = render.render({"blocks": [{"type": "image", "data": base64.b64encode(big.getvalue()).decode()}]})
        self.assertEqual(out.width, protocol.WIDTH)
        self.assertAlmostEqual(out.height / protocol.WIDTH, 0.75, delta=0.02)
        # camera outline strength is a setting and reaches the print
        code, body, _ = self.call("/api/settings", {"camera_weight": 3})
        self.assertEqual((code, json.loads(body)["camera_weight"]), (200, 3))
        self.assertEqual(self.call("/api/settings", {"camera_weight": 4})[0], 400)
        if ai.available("lineart"):
            img = Image.open(io.BytesIO(self._jpeg()))
            with ai._lock:
                ai._drawings.clear()            # the server's background drawing may have drawn it already
            t = time.perf_counter()
            first = ai.draw(img, "lineart", 900)
            once = time.perf_counter() - t
            t = time.perf_counter()
            again = ai.draw(img, "lineart", 900)
            self.assertLess(time.perf_counter() - t, once / 5)                 # remembered, not redrawn
            self.assertEqual(first.tobytes(), again.tobytes())
            self.assertEqual(first.width, ai.MAX_MODEL_WIDTH)                  # capped

    def _alerts(self):
        notes = []
        self.httpd.service._notify = lambda title, message: notes.append((title, message))
        return notes

    def test_out_of_paper_waits_alerts_once_and_prints_when_fixed(self):
        service = self.httpd.service
        service.RETRY_EVERY = 0.1
        notes = self._alerts()
        printer = service.printer
        printer.transport.state_flags = 0x01                               # A3 says: no paper
        code, body, _ = self.call("/api/print/text?wait=1", b"hello")
        job = json.loads(body)["job"]
        self.assertEqual((code, job["status"]), (202, "waiting"))          # ?wait answers once it's waiting
        self.assertIn("out of paper", job["error"])
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "simulated-paper")), [])   # nothing was sent
        status = json.loads(self.call("/api/status")[1])
        self.assertEqual(status["printer"]["last_status"]["problems"], ["out_of_paper"])
        self.assertNotEqual(status["printer"]["state"], "error")           # reachable, just not ready
        self.assertEqual([j["status"] for j in status["queue"]], ["waiting"])
        time.sleep(0.4)                                                    # a few more tries: still one alert
        self.assertEqual(notes, [("Printer: out of paper or the lid is open",
                                  "Waiting prints go out once it's fixed (for up to 30 minutes).")])
        printer.transport.state_flags = 0                                  # paper in
        for _ in range(50):
            if json.loads(self.call("/api/jobs/" + job["id"])[1])["status"] == "done":
                break
            time.sleep(0.1)
        self.assertEqual(json.loads(self.call("/api/jobs/" + job["id"])[1])["status"], "done")
        self.assertEqual(len(os.listdir(os.path.join(self.tmp.name, "simulated-paper"))), 1)
        self.assertEqual(notes[-1][0], "Printer: printed after all")
        self.assertEqual(catproto.problems(0x0A), ["lid_open", "low_battery"])

    def test_waiting_print_can_be_cancelled_and_retry_can_be_off(self):
        service = self.httpd.service
        service.RETRY_EVERY = 5
        notes = self._alerts()
        service.printer.transport.state_flags = 0x02                       # lid open
        job = json.loads(self.call("/api/print/text?wait=1", b"hello")[1])["job"]
        self.assertEqual(job["status"], "waiting")
        self.call(f"/api/jobs/{job['id']}/cancel", method="POST")
        for _ in range(30):
            if json.loads(self.call("/api/jobs/" + job["id"])[1])["status"] == "cancelled":
                break
            time.sleep(0.1)
        self.assertEqual(json.loads(self.call("/api/jobs/" + job["id"])[1])["status"], "cancelled")
        self.cfg["retry_minutes"] = 0                                      # off: fails at once, says so
        code, body, _ = self.call("/api/print/text?wait=1", b"hello")
        self.assertEqual((code, json.loads(body)["job"]["status"]), (502, "failed"))
        self.assertEqual(notes[-1], ("Printer: the lid is open", "The print failed; reprint from History in the web app."))

    def test_camera_print_answers_before_drawing_and_previews_are_kept(self):
        code, body, _ = self.call("/api/camera/print", self._jpeg(), headers={"X-Filename": "IMG_20261030_120000.jpg"})
        reply = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(reply["job"]["height"], 0)                        # drawn by the print worker, later
        job_id = reply["job"]["id"]
        for _ in range(100):
            job = json.loads(self.call("/api/jobs/" + job_id)[1])
            if job["status"] not in ("queued", "printing"):
                break
            time.sleep(0.1)
        self.assertEqual(job["status"], "done")
        self.assertGreater(job["height"], 0)
        name = reply["name"]
        first = self.call(f"/api/camera/photo/{name}/preview", {"style": "photo"})
        t = time.perf_counter()
        again = self.call(f"/api/camera/photo/{name}/preview", {"style": "photo"})
        self.assertLess(time.perf_counter() - t, 0.2)
        self.assertEqual(first[1], again[1])
        other = self.call(f"/api/camera/photo/{name}/preview", {"style": "line"})
        self.assertNotEqual(first[1], other[1])

    def test_photos_and_pages_are_cacheable(self):
        self.call("/api/camera/upload", self._jpeg(), headers={"X-Filename": "a.jpg"})
        photo = json.loads(self.call("/api/camera/photos")[1])["photos"][0]
        self.assertIn("?v=", photo["thumb"])
        _, _, headers = self.call(photo["thumb"])
        self.assertEqual(headers.get_all("Cache-Control"), ["private, max-age=31536000, immutable"])
        _, _, headers = self.call(photo["download"])
        self.assertIn("attachment", headers["Content-Disposition"])
        code, _, headers = self.call("/app.js")
        self.assertEqual(code, 200)
        code, body, _ = self.call("/app.js", headers={"If-None-Match": headers["ETag"]})
        self.assertEqual((code, body), (304, b""))

    def test_camera_refuses_bad_names_and_bodies(self):
        self.assertEqual(self.call("/api/camera/upload", b"not a jpeg", headers={"X-Filename": "a.jpg"})[0], 400)
        name = json.loads(self.call("/api/camera/upload", self._jpeg(),
                                    headers={"X-Filename": "../../evil.jpg"})[1])["name"]
        self.assertEqual(name, "evil.jpg")                                 # paths are stripped from names
        for bad in ("/api/camera/photo/..%2Fconfig.json", "/api/camera/photo/.thumbs", "/api/camera/thumb/x.png"):
            self.assertEqual(self.call(bad)[0], 404, bad)

    def test_framed_print_is_taller_with_a_border(self):
        b64 = base64.b64encode(self._jpeg()).decode()
        plain = render.render({"blocks": [{"type": "image", "data": b64, "style": "line"}]})
        framed = render.render({"blocks": [{"type": "image", "data": b64, "style": "line", "frame": True,
                                            "caption": "Hi {year}"}]}).convert("L")
        self.assertEqual(framed.width, protocol.WIDTH)
        self.assertGreater(framed.height, plain.height * 0.8)
        column = [framed.getpixel((7, y)) for y in range(10, framed.height - 10)]
        self.assertTrue(all(v == 0 for v in column))                       # the frame's left edge is ink

    def test_sideways_prints_are_bigger(self):
        b64 = base64.b64encode(self._jpeg()).decode()              # 640x480 landscape
        upright = render.render({"blocks": [{"type": "image", "data": b64, "style": "photo", "frame": True}]})
        sideways = render.render({"blocks": [{"type": "image", "data": b64, "style": "photo", "frame": True,
                                              "sideways": True, "caption": "{date}"}]})
        bare = render.render({"blocks": [{"type": "image", "data": b64, "style": "photo", "sideways": True}]})
        self.assertEqual({upright.width, sideways.width, bare.width}, {protocol.WIDTH})
        self.assertGreater(sideways.height, upright.height)
        self.assertGreater(bare.height, sideways.height)
        self.assertAlmostEqual(bare.height / (protocol.WIDTH - 12), 640 / 480, delta=0.02)   # aspect kept
        portrait = Image.new("RGB", (300, 400), "white")
        buf = io.BytesIO()
        portrait.save(buf, "PNG")
        tall = render.render({"blocks": [{"type": "image", "data": base64.b64encode(buf.getvalue()).decode(),
                                          "sideways": True}]})
        self.assertAlmostEqual(tall.height / protocol.WIDTH, 400 / 300, delta=0.02)          # upright as is

    def test_camera_settings(self):
        code, body, _ = self.call("/api/settings", {"camera_style": "coloring", "camera_frame": False,
                                                    "camera_caption": "Photo by me", "camera_detail": 0.7})
        self.assertEqual(code, 200, body)
        s = json.loads(body)
        self.assertEqual((s["camera_style"], s["camera_frame"], s["camera_caption"], s["camera_detail"]),
                         ("coloring", False, "Photo by me", 0.7))
        for bad in ({"camera_style": "crayon"}, {"camera_frame": "yes"}, {"camera_caption": "x" * 61},
                    {"camera_detail": 2}):
            self.assertEqual(self.call("/api/settings", bad)[0], 400, bad)

    def test_bad_requests(self):
        self.assertEqual(self.call("/api/print", b"{not json", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.call("/api/print", {"blocks": [{"type": "nope"}]})[0], 400)
        self.assertEqual(self.call("/api/print/text", b"   ")[0], 400)
        self.assertEqual(self.call("/api/jobs/nope/reprint", {})[0], 404)
        self.assertEqual(self.call("/api/wat")[0], 404)
        self.assertEqual(self.call("/../config.json")[0], 404)

    def test_other_web_sites_cannot_print(self):
        code, _, _ = self.call("/api/print/text", b"spam", headers={"Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        host = self.base.split("//")[1]
        code, _, _ = self.call("/api/print/text", b"ok", headers={"Origin": f"http://{host}"})
        self.assertEqual(code, 202)

    def test_token(self):
        self.stop()
        self.cfg["token"] = "s3cret"
        self.start()
        self.assertEqual(self.call("/api/status")[0], 401)
        self.assertEqual(self.call("/api/status?token=s3cret")[0], 200)
        self.assertEqual(self.call("/api/print/text", b"x", headers={"Authorization": "Bearer s3cret"})[0], 202)
        self.assertEqual(self.call("/")[0], 200)  # the app itself loads, then asks for the token

    def test_history_survives_restart(self):
        self.call("/api/print/text?wait=1", b"remember me")
        self.stop()
        self.start()
        jobs = json.loads(self.call("/api/jobs")[1])["jobs"]
        self.assertEqual(jobs[0]["title"], "remember me")
        code, body, _ = self.call(f"/api/jobs/{jobs[0]['id']}/reprint?wait=1", {})
        self.assertEqual((code, json.loads(body)["job"]["status"]), (200, "done"))


class ToneTests(unittest.TestCase):
    def test_tone_curve_spans_black_to_paper_without_reversals(self):
        from ctp500 import styles
        for level in (2, 3, 4, 5):
            curve = styles.tone_curve(level)
            self.assertEqual((curve[0], curve[255]), (0, 255))
            self.assertTrue(all(a <= b for a, b in zip(curve, curve[1:])), level)
        # light skin (gray ~200) no longer prints as bare paper: it gets dots (Atkinson prints none above ~229)
        self.assertLess(styles.tone_curve(2)[200], 215)
        # normal spreads its dots more, so the same tone needs fewer of them than at light
        self.assertGreater(styles.tone_curve(3)[128], styles.tone_curve(2)[128])

    def test_photo_look_picks_tones_and_darkness(self):
        photo = {"blocks": [{"type": "image", "data": png_b64()}]}
        drawing = {"blocks": [{"type": "image", "data": png_b64(), "style": "line"}, {"type": "text", "text": "x"}]}
        punchy = {"darkness": "normal", "photo_look": "punchy"}
        even = {"darkness": "normal", "photo_look": "even"}
        # punchy (the default) prints at the normal darkness; even prints photos at light
        self.assertEqual(render.with_defaults(dict(photo), punchy)["darkness"], "normal")
        self.assertEqual(render.with_defaults(dict(photo), {"darkness": "normal"})["photo_look"], "punchy")
        self.assertEqual(render.with_defaults(dict(photo), even)["darkness"], "light")
        self.assertEqual(render.with_defaults(dict(drawing), even)["darkness"], "normal")
        self.assertEqual(render.with_defaults({"blocks": [], "darkness": "auto"}, even)["darkness"], "normal")
        self.assertEqual(render.with_defaults(dict(photo, darkness="dark"), even)["darkness"], "dark")
        a = render.render(render.with_defaults(dict(photo), punchy))
        b = render.render(render.with_defaults(dict(photo), even))
        self.assertNotEqual(a.tobytes(), b.tobytes())


if __name__ == "__main__":
    unittest.main()


class FaceTests(unittest.TestCase):
    def setUp(self):
        from ctp500 import faces
        self.faces = faces
        self._find = faces.find

    def tearDown(self):
        self.faces.find = self._find

    def face(self, x, y, w):
        return self.faces.Face(x, y, w, w * 1.2, 0.9, ((x + w * 0.3, y + w * 0.5), (x + w * 0.7, y + w * 0.5)))

    def test_zoom_box_keeps_the_people_and_the_shape(self):
        two = [self.face(400, 250, 90), self.face(560, 280, 80)]
        l, t, r, b = self.faces.zoom_box((1024, 768), two)
        self.assertAlmostEqual((r - l) / (b - t), 1024 / 768, delta=0.02)        # the photo's own shape
        self.assertGreaterEqual(r - l, 1024 / self.faces.ZOOM_MAX - 1)          # no closer than 2x
        for f in two:                                                           # heads inside
            self.assertTrue(l <= f.x and f.x + f.w <= r and t <= f.y and f.y + f.h <= b)
        self.assertTrue(0 <= l and r <= 1024 and 0 <= t and b <= 768)
        self.assertIsNone(self.faces.zoom_box((1024, 768), [self.face(300, 150, 400)]))   # already close
        self.assertIsNone(self.faces.zoom_box((1024, 768), []))
        near_edge = self.faces.zoom_box((1024, 768), [self.face(950, 20, 60)])
        self.assertTrue(near_edge[2] <= 1024 and near_edge[1] >= 0)                # pushed back inside
        far = self.face(60, 60, 20)                                              # someone far behind: ignored
        self.assertEqual(self.faces.zoom_box((1024, 768), two + [far]), self.faces.zoom_box((1024, 768), two))

    def test_render_zoom_and_face_tones_use_the_faces(self):
        from ctp500 import styles
        img = Image.new("RGB", (1024, 768), (200, 200, 200))
        ImageDraw.Draw(img).rectangle([450, 300, 549, 419], fill=(40, 40, 40))   # a dark "face"
        self.faces.find = lambda picture: [self.face(450 * picture.width / 1024, 300 * picture.width / 1024,
                                                     100 * picture.width / 1024)]
        zoomed = render._zoom_faces(img)
        self.assertTrue(512 <= zoomed.width < 1024 * 0.85 and abs(zoomed.width / zoomed.height - 4 / 3) < 0.02)
        gray = img.convert("L")
        lifted = styles._face_tones(gray, img)
        self.assertGreater(lifted.getpixel((500, 360)), gray.getpixel((500, 360)) + 20)
        self.faces.find = lambda picture: []
        self.assertIs(styles._face_tones(gray, img), gray)
        self.assertIs(render._zoom_faces(img), img)

    def test_overlapping_finds_are_merged(self):
        import numpy as np
        boxes = np.array([[10, 10, 50, 50], [12, 11, 50, 50], [200, 200, 40, 40]], dtype=np.float32)
        self.assertEqual(sorted(self.faces._nms(boxes, np.array([0.9, 0.8, 0.85]))), [0, 2])


class StuckPixelTests(unittest.TestCase):
    def shot(self, size=(1024, 768), spot=True):
        from ctp500 import camera
        w, h = size
        img = Image.new("RGB", size)
        draw = ImageDraw.Draw(img)
        for x in range(w):                                   # a soft gradient, like a wall
            draw.line([(x, 0), (x, h)], fill=(60 + x * 80 // w, 50, 70))
        if spot:
            cx, cy = round(camera.STUCK_PIXEL[0] * w), round(camera.STUCK_PIXEL[1] * h)
            draw.rectangle([cx, cy - 2, cx + 1, cy + 2], fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        return buf.getvalue()

    def spot_left(self, data, size=(1024, 768)):
        from ctp500 import camera
        img = Image.open(io.BytesIO(data)).convert("L")
        cx, cy = round(camera.STUCK_PIXEL[0] * size[0]), round(camera.STUCK_PIXEL[1] * size[1])
        return max(img.crop((cx - 6, cy - 6, cx + 7, cy + 7)).getdata()) - img.getpixel((cx - 20, cy))

    def test_camera_shots_lose_the_spot_and_nothing_else_changes(self):
        from ctp500 import camera
        data = self.shot()
        self.assertGreater(self.spot_left(data), 100)
        fixed = camera.fix_stuck_pixel(data, camera.STUCK_PIXEL)
        self.assertLess(self.spot_left(fixed), 15)
        self.assertEqual(camera.fix_stuck_pixel(data, camera.STUCK_PIXEL), fixed)               # same bytes every time
        clean = self.shot(spot=False)
        self.assertEqual(camera.fix_stuck_pixel(clean, camera.STUCK_PIXEL), clean)              # no spot: untouched
        big = self.shot((2048, 1536))
        self.assertEqual(camera.fix_stuck_pixel(big, camera.STUCK_PIXEL), big)  # phone-sized: untouched
        self.assertEqual(camera.fix_stuck_pixel(data), data)                # not set up: untouched

    def test_column_streaks_come_out_of_photos_only(self):
        import numpy as np
        from unittest import mock
        from ctp500 import camera
        w, h = 1600, 400                                     # the camera's width: the bands are measured for it
        base = Image.new("RGB", (w, h))
        draw = ImageDraw.Draw(base)
        for y in range(h):                                   # a gradient and a dark disc: content, not streaks
            draw.line([(0, y), (w, y)], fill=(80 + y * 100 // h, 70, 90))
        draw.ellipse([500, 80, 800, 330], fill=(40, 30, 50))
        rng = np.random.default_rng(7)
        a = np.asarray(base, dtype=np.int16).copy()
        for x in rng.choice(w, 160, replace=False):          # the sensor's columns, the same all the way down
            a[:, x] += int(rng.choice((-1, 1)) * rng.integers(12, 40))
        a += np.round(6 * np.sin(np.arange(w) * 2 * np.pi / 60)).astype(np.int16)[None, :, None]  # + wide bands
        a += rng.integers(-3, 4, a.shape, dtype=np.int16)    # and some grain, like a real shot
        streaky = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")

        def spread(img):
            return camera._column_offsets(np.asarray(img, dtype=np.float32), slice(None)).std()

        def bands(img):
            return camera._bands(np.asarray(img, dtype=np.float32), 1).std()
        self.assertGreater(spread(streaky), 4)
        fixed = camera.destripe(streaky)
        self.assertLess(spread(fixed), 1)
        self.assertGreater(bands(streaky), 2)
        self.assertLess(bands(fixed), bands(streaky) / 3)
        self.assertIs(camera.destripe(base), base)           # nothing to take out: the same picture back
        # only photos (JPEG) go through it: a PNG drawing with fine vertical lines is the drawing
        calls = []
        with mock.patch.object(camera, "destripe", lambda img: calls.append(img.size) or img):
            for fmt in ("JPEG", "PNG"):
                buf = io.BytesIO()
                streaky.save(buf, fmt)
                render.render({"blocks": [{"type": "image", "style": "floyd",
                                           "data": base64.b64encode(buf.getvalue()).decode()}]})
        self.assertEqual(calls, [(w, h)])

    def test_gallery_stores_the_fixed_shot_once(self):
        from ctp500 import camera
        with tempfile.TemporaryDirectory() as tmp:
            gallery = camera.CameraGallery(tmp, camera.STUCK_PIXEL)
            name = gallery.save(self.shot(), "IMG00099.jpg")
            self.assertEqual(gallery.save(self.shot(), "IMG00099.jpg"), name)  # the camera re-sending it
            self.assertLess(self.spot_left(gallery.read(name)), 15)


class LithophaneTests(unittest.TestCase):
    def photo(self):
        """Bright on the left, dark on the right, with a mid-gray band at the top: shows orientation."""
        img = Image.new("RGB", (400, 300), (20, 20, 20))
        ImageDraw.Draw(img).rectangle([0, 0, 199, 299], fill=(240, 240, 240))
        ImageDraw.Draw(img).rectangle([0, 0, 399, 40], fill=(128, 128, 128))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=95)
        return buf.getvalue()

    def test_thickness_is_whole_layers_and_reads_the_right_way(self):
        from ctp500 import litho
        import numpy as np
        t, res = litho.heights(self.photo(), 60)
        w, h = litho.dimensions(t, res)
        self.assertEqual((round(w, 3), round(h, 3)), (60.0, 44.8))                  # 0.4 mm steps
        layers = np.unique(np.round((t - litho.FIRST_LAYER_MM) / litho.LAYER_MM, 4))
        self.assertTrue(np.allclose(layers, np.round(layers)))                       # whole layers only
        self.assertAlmostEqual(float(t.min()), litho.MIN_MM, places=4)
        self.assertAlmostEqual(float(t.max()), litho.MAX_MM, places=4)
        mid = t.shape[0] * 2 // 3
        self.assertLess(t[mid, 20], t[mid, -20])                                     # bright left = thin left
        edge = round(litho.FRAME_MM / res)
        self.assertTrue((t[:edge] == t[0, 0]).all() and (t[:, -edge:] == t[0, 0]).all())   # the frame

    def test_meshes_are_closed_and_face_outwards(self):
        from ctp500 import litho
        import numpy as np
        t, res = litho.heights(self.photo(), 60)
        for shape in litho.SHAPES:
            verts, tris = litho.mesh(t, res, shape)
            edges = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
            keys = edges[:, 0] * len(verts) + edges[:, 1]
            self.assertEqual(len(np.unique(keys)), len(keys), shape)                # no edge used twice one way
            self.assertTrue(np.isin(edges[:, 1] * len(verts) + edges[:, 0], keys).all(), shape)  # closed
            v = verts[tris]
            volume = np.einsum("ij,ij->i", v[:, 0], np.cross(v[:, 1], v[:, 2])).sum() / 6
            self.assertGreater(volume, 0, shape)                                     # outward
            self.assertGreaterEqual(verts.min(), -1e-9)
        verts, _ = litho.mesh(t, res, "flat")
        self.assertAlmostEqual(float(verts[:, 2].max()), litho.MAX_MM, places=4)
        verts, _ = litho.mesh(t, res, "curved")
        self.assertAlmostEqual(float(np.ptp(verts[:, 2])), 44.8, places=3)             # stands up
        with self.assertRaises(ValueError):
            litho.mesh(t, res, "cube")

    def test_relief_plaque(self):
        from ctp500 import ai, litho
        import numpy as np
        if not ai.available("depth"):
            self.skipTest("no depth model")
        t, res = litho.relief_heights(self.photo(), 60)
        edge = round(litho.FRAME_MM / res)
        rim = litho.RELIEF_BASE_MM + litho.RELIEF_RIM_MM
        self.assertTrue(np.allclose(t[:edge], rim) and np.allclose(t[:, -edge:], rim))  # straight walls
        self.assertGreater(t.max(), rim + 1)                                            # something stands out
        self.assertGreaterEqual(t.min(), litho.RELIEF_BASE_MM * 0.6 - 1e-6)
        png = Image.open(io.BytesIO(litho.relief_preview(t, res, 200)))
        self.assertEqual(png.width, 200)


class OffloadTests(unittest.TestCase):
    def test_round_trip_timeout_and_no_helper(self):
        from ctp500 import offload
        o = offload.Offload()
        with self.assertRaises(offload.Unavailable):
            o.run("draw", b"x")                                         # nobody polling
        self.assertIsNone(o.next(["draw"], "pc", wait=0.05))            # a poll with nothing to do
        self.assertTrue(o.online("draw") and not o.online("slice"))

        def helper():
            job = o.next(["draw"], "pc", wait=2)
            o.result(job["id"], job["data"][::-1], {"ok": 1})
        threading.Thread(target=helper, daemon=True).start()
        self.assertEqual(o.run("draw", b"abc", timeout=2), (b"cba", {"ok": 1}))
        with self.assertRaises(offload.Unavailable):
            o.run("draw", b"abc", timeout=0.1)                          # seen, but nobody takes it
        self.assertEqual(len(o._queue), 0)                               # the unanswered job is gone

        def failing():
            job = o.next(["draw"], "pc", wait=2)
            o.result(job["id"], error="no GPU")
        threading.Thread(target=failing, daemon=True).start()
        with self.assertRaises(RuntimeError):
            o.run("draw", b"abc", timeout=2)
