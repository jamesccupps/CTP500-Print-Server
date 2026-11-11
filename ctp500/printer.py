"""One printer, one job at a time: connects when there's work, stays connected a little while for the
next job, then lets go so the printer can sleep and the phone app can still reach it.

Two command dialects exist for CTP500 printers: "cat" (framed 51 78 packets; "Mini Printer" units) and
"escpos" (the CTP500 units the other projects describe). protocol="auto" asks the printer on connect.
The Phomemo M02 Pro speaks "phomemo" (phomemo.py).
"""

from __future__ import annotations

import threading
import time

from PIL import Image

from . import catproto, phomemo, printers, protocol, raster
from .transport import BluetoothTransport, Cancelled, SimulatedTransport, TransportError

DIALECTS = ("auto", "cat", "escpos", "phomemo")
LINE_DOTS = 32  # "feed" counts text lines of about 4 mm, in both CTP500 dialects


class PrinterNotReady(TransportError):
    """The printer answered, but says it can't print (out of paper, lid open)."""

    def __init__(self, problems: list[str], texts: dict | None = None):
        self.problems = problems
        self.texts = texts or catproto.PROBLEM_TEXT
        super().__init__("the printer says " + " and ".join(self.texts.get(p, p) for p in problems))

    def text(self) -> str:
        return " and ".join(self.texts.get(p, p) for p in self.problems)


class Printer:
    def __init__(self, transport, keep_alive: float = 45.0, connect_attempts: int = 3, dialect: str = "auto",
                 model: printers.Model | None = None, printer_id: str = "", label: str = "", feed: int | None = None,
                 paper: printers.Paper | None = None):
        self.model = model or printers.CTP500
        self.default_feed = self.model.feed if feed is None else int(feed)   # ~4 mm lines after a print
        self.paper = paper or self.model.paper
        if dialect == "auto" or not dialect:
            dialect = self.model.dialects[0]
        if dialect not in DIALECTS or dialect not in self.model.dialects:
            raise ValueError(f"protocol for the {self.model.label} must be one of {', '.join(self.model.dialects)}")
        self.id = printer_id or self.model.key
        self.label = label or self.model.short
        self.transport = transport
        self.keep_alive = keep_alive
        self.connect_attempts = max(1, connect_attempts)
        self.wanted_dialect = dialect
        self.dialect: str | None = None if dialect == "auto" else dialect
        self.lock = threading.RLock()
        self.state = "idle"            # idle | connecting | connected | printing | error
        self.last_error: str | None = None
        self.last_status: dict | None = None
        self.last_used = 0.0
        self.printed_dots = 0          # paper used since start, in dots
        self._closer: threading.Timer | None = None

    @property
    def name(self) -> str:
        return self.transport.name

    @property
    def problem_texts(self) -> dict:
        return phomemo.PROBLEM_TEXT if self.wanted_dialect == "phomemo" else catproto.PROBLEM_TEXT

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "model": self.model.key,
            "name": self.name,
            "simulated": isinstance(self.transport, SimulatedTransport),
            "address": getattr(self.transport, "address", None),
            "channel": getattr(self.transport, "channel", None),
            "protocol": self.dialect or self.wanted_dialect,
            "width": self.paper.width,
            "dpi": self.paper.dpi,
            "paper_mm": self.model.paper_mm(self.paper),
            "state": self.state,
            "connected": self.transport.is_open,
            "last_error": self.last_error,
            "last_status": self.last_status,
            "last_used": self.last_used or None,
            "paper_used_mm": self.paper.mm(self.printed_dots),
        }

    # -------------------------------------------------------------- connection

    def connect(self) -> None:
        with self.lock:
            self._cancel_close()
            if self.transport.is_open:
                return
            self.state = "connecting"
            error = None
            for attempt in range(self.connect_attempts):
                try:
                    self.transport.open()
                    break
                except TransportError as exc:
                    error = exc
                    if attempt + 1 < self.connect_attempts:
                        time.sleep(1.5 * (attempt + 1))
            else:
                self.state, self.last_error = "error", str(error)
                raise error
            if self.dialect is None or self._guessed:
                self._detect()
            self.state, self.last_error = "connected", None

    _guessed = False    # escpos was assumed from silence: ask again on the next connection

    def _detect(self) -> None:
        """Ask in the cat dialect; anything that answers in it speaks it, otherwise assume ESC/POS. A printer that
        just woke up can miss the first question, so it's asked twice, and a silent answer isn't trusted for good
        (a cat printer taken for ESC/POS prints nothing at all)."""
        for timeout in (1.5, 2.5):
            reply = self.transport.request(catproto.state_request(), timeout=timeout)
            if any(cmd == catproto.DEVICE_STATE for cmd, _ in catproto.parse_packets(reply)):
                self.dialect, self._guessed = "cat", False
                self.last_status = dict(catproto.parse_state(reply), at=time.time())
                return
        self.dialect, self._guessed = "escpos", True

    def _ready_check(self) -> None:
        """Ask the printer whether it can print before sending a job: out of paper or an open lid would
        otherwise swallow the job silently."""
        if self.dialect == "phomemo":
            reply = self.transport.request(phomemo.READY_QUERIES, timeout=1.5)
            if not phomemo.parse_replies(reply):
                return                  # no answer: print anyway
            self.last_status = dict(phomemo.parse_state(reply), at=time.time())
            blocking = phomemo.faults(reply)
            if blocking:
                raise PrinterNotReady(blocking, phomemo.PROBLEM_TEXT)
            return
        if self.dialect != "cat":
            return
        # connect() may have just asked; a problem is always asked again (paper may have just gone in)
        fresh = (self.last_status and time.time() - self.last_status.get("at", 0) < 2
                 and not self.last_status.get("problems"))
        if not fresh:
            reply = self.transport.request(catproto.state_request(), timeout=1.5)
            if not any(cmd == catproto.DEVICE_STATE for cmd, _ in catproto.parse_packets(reply)):
                return                  # no answer: print anyway, as before
            self.last_status = dict(catproto.parse_state(reply), at=time.time())
        blocking = [p for p in self.last_status.get("problems", []) if p in catproto.BLOCKING]
        if blocking:
            raise PrinterNotReady(blocking)

    def disconnect(self) -> None:
        with self.lock:
            self._cancel_close()
            self.transport.close()
            if self.state != "error":
                self.state = "idle"

    def _schedule_close(self) -> None:
        self._cancel_close()
        if self.keep_alive <= 0:
            self.disconnect()
            return
        self._closer = threading.Timer(self.keep_alive, self._idle_close)
        self._closer.daemon = True
        self._closer.start()

    def _cancel_close(self) -> None:
        if self._closer:
            self._closer.cancel()
            self._closer = None

    def _idle_close(self) -> None:
        if self.lock.acquire(blocking=False):
            try:
                if time.monotonic() - self.last_used >= self.keep_alive - 0.5:
                    self.transport.close()
                    self.state = "idle"
            finally:
                self.lock.release()

    # -------------------------------------------------------------- work

    def _job(self, bw: Image.Image, darkness, feed: int) -> bytes:
        paper = self.paper
        if paper.head_width != paper.width:     # a print narrower than the head: put it where the paper is
            if bw.mode != "1":
                bw = bw.convert("1", dither=raster.NO_DITHER)
            strip = Image.new("1", (paper.head_width, bw.height), 1)
            strip.paste(bw.crop((0, 0, paper.width, bw.height)), (paper.left, 0))
            bw = strip
        rows = raster.pack(bw, paper.head_width)
        if self.dialect == "phomemo":
            return phomemo.build_job(rows, bw.height, self.paper.row_bytes, darkness, feed)
        if self.dialect == "cat":
            return catproto.build_job(rows, bw.height, darkness, max(0, int(feed)) * LINE_DOTS)
        return protocol.build_job(rows, bw.height, darkness, feed)

    def _flow(self):
        if self.dialect == "cat":
            return catproto.flow_signal
        if self.dialect == "phomemo":
            def watch(data: bytes):
                """The M02 Pro says by itself when the paper runs out or the lid opens: stop sending."""
                if data:
                    blocking = phomemo.faults(data)
                    if blocking:
                        self.last_status = dict(phomemo.parse_state(data), at=time.time())
                        raise PrinterNotReady(blocking, phomemo.PROBLEM_TEXT)
                return None
            return watch
        return None

    def print_image(self, bw: Image.Image, darkness=None, feed: int = 2, copies: int = 1,
                    cancel: threading.Event | None = None, progress=None) -> None:
        copies = max(1, min(20, int(copies)))
        with self.lock:
            self.connect()
            job = self._job(bw, darkness, feed)
            line_dots = phomemo.LINE_DOTS if self.dialect == "phomemo" else LINE_DOTS
            sending = False
            try:
                self._ready_check()
                self.state = "printing"
                for n in range(copies):
                    def each(sent, total, n=n):
                        if progress:
                            progress((n * total + sent) / (copies * total))
                    started = time.monotonic()
                    sending = True
                    self.transport.write(job, cancel, each, self._flow())
                    sending = False
                    self._wait_done(bw.height, started)
                    self.printed_dots += bw.height + feed * line_dots
                self.state, self.last_error = "connected", None
            except Cancelled:
                self._abandon(sending)
                self.state = "connected" if self.transport.is_open else "idle"
                raise
            except PrinterNotReady as exc:   # reachable and answering, just not able to print
                self._abandon(sending)
                self.state, self.last_error = "connected" if self.transport.is_open else "idle", str(exc)
                raise
            except TransportError as exc:
                self.state, self.last_error = "error", str(exc)
                raise
            finally:
                self.last_used = time.monotonic()
                if self.transport.is_open:
                    self._schedule_close()

    def _abandon(self, sending: bool) -> None:
        """A raster stopped halfway leaves the M02 Pro waiting for the rest of it (it would read the next job as
        dots): hang up, so it starts over."""
        if sending and self.dialect == "phomemo":
            self.transport.close()

    def _wait_done(self, height: int, started: float) -> None:
        """Cat jobs end with a state request, answered once the printer reaches it: that's 'printed'. The M02 Pro
        says 1A 0F 0C when a print is done (seen 2026-11-11; the sources didn't list it as an answer to a print);
        it gets at most the time it needs for the rows (counted from the first byte sent) to say so, listening
        for paper-out and lid-open meanwhile."""
        if self.dialect == "phomemo":
            until = max(started + phomemo.print_seconds(height), time.monotonic()) + 1.0

            def news(data: bytes) -> bool:
                return bool(phomemo.faults(data)) or any(op == phomemo.RESULT for op, _ in phomemo.parse_replies(data))
            reply = self.transport.read_until(news, timeout=until - time.monotonic())
            if reply:
                self.last_status = dict(phomemo.parse_state(reply), at=time.time())
                blocking = phomemo.faults(reply)
                if blocking:
                    raise PrinterNotReady(blocking, phomemo.PROBLEM_TEXT)
            return
        if self.dialect != "cat":
            return
        def answered(data: bytes) -> bool:
            return any(cmd == catproto.DEVICE_STATE for cmd, _ in catproto.parse_packets(data))
        reply = self.transport.read_until(answered, timeout=8 + height / 60)
        if answered(reply):
            self.last_status = dict(catproto.parse_state(reply), at=time.time())
            blocking = [p for p in self.last_status.get("problems", []) if p in catproto.BLOCKING]
            if blocking:            # ran out (or the lid came open) partway through
                raise PrinterNotReady(blocking)

    def feed(self, lines: int = 3) -> None:
        with self.lock:
            self.connect()
            try:
                if self.dialect == "phomemo":
                    self.transport.write(phomemo.INIT + phomemo.feed_command(int(lines) * phomemo.LINE_DOTS))
                elif self.dialect == "cat":
                    self.transport.write(catproto.feed_command(int(lines) * LINE_DOTS))
                else:
                    self.transport.write(protocol.INIT + protocol.feed_command(lines))
            finally:
                self.last_used = time.monotonic()
                if self.transport.is_open:
                    self._schedule_close()

    def check(self) -> dict:
        """Connect, ask for status, and report what came back."""
        with self.lock:
            self.connect()
            try:
                if self.dialect == "phomemo":
                    status = phomemo.parse_state(self.transport.request(phomemo.STATUS_QUERIES, timeout=2.0))
                elif self.dialect == "cat":
                    reply = self.transport.request(catproto.state_request())
                    reply += self.transport.request(catproto.info_request())
                    status = catproto.parse_state(reply)
                else:
                    status = protocol.parse_status(self.transport.request(protocol.STATUS_REQUEST))
                self.last_status = dict(status, protocol=self.dialect, at=time.time())
                return self.last_status
            except TransportError as exc:
                self.state, self.last_error = "error", str(exc)
                raise
            finally:
                self.last_used = time.monotonic()
                if self.transport.is_open:
                    self._schedule_close()


def make_printer(target: str, data_dir: str, channel: int | None = 1, keep_alive: float = 45.0,
                 dialect: str = "auto", model: str | printers.Model | None = None, printer_id: str = "",
                 label: str = "", feed: int | None = None, paper=None) -> Printer:
    """target is a Bluetooth address (AA:BB:CC:DD:EE:FF) or "sim". channel None: the model's usual one
    (0 = find it). paper: the roll width in mm (None: the model's usual paper)."""
    if not target:
        raise ValueError("no printer set: give its Bluetooth address, or 'sim' for the simulated printer")
    model = model if isinstance(model, printers.Model) else printers.model(model)
    options = dict(keep_alive=keep_alive, dialect=dialect, model=model, printer_id=printer_id, label=label, feed=feed,
                   paper=model.paper_for(paper))
    if target.lower() in ("sim", "simulated", "simulate"):
        import os
        if model.dialects == ("phomemo",):
            sim_dialect = "phomemo"
        else:
            sim_dialect = "escpos" if dialect == "escpos" else "cat"
        folder = "simulated-paper" if model is printers.CTP500 else f"simulated-paper-{model.key}"
        name = "simulated printer" if model is printers.CTP500 else f"simulated {model.short}"
        return Printer(SimulatedTransport(os.path.join(data_dir, folder), dialect=sim_dialect, name=name), **options)
    return Printer(BluetoothTransport(target, model.channel if channel is None else channel,
                                      chunk_size=model.chunk_size, chunk_delay=model.chunk_delay), **options)
