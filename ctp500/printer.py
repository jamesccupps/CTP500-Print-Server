"""One printer, one job at a time: connects when there's work, stays connected a little while for the
next job, then lets go so the printer can sleep and the phone app can still reach it.

Two command dialects exist for these printers: "cat" (framed 51 78 packets; "Mini Printer" units) and
"escpos" (the CTP500 units the other projects describe). protocol="auto" asks the printer on connect.
"""

from __future__ import annotations

import threading
import time

from PIL import Image

from . import catproto, protocol, raster
from .transport import BluetoothTransport, Cancelled, SimulatedTransport, TransportError

DIALECTS = ("auto", "cat", "escpos")
LINE_DOTS = 32  # "feed" counts text lines of about 4 mm, in both dialects


class PrinterNotReady(TransportError):
    """The printer answered, but says it can't print (out of paper, lid open)."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("the printer says " + " and ".join(catproto.PROBLEM_TEXT.get(p, p) for p in problems))


class Printer:
    def __init__(self, transport, keep_alive: float = 45.0, connect_attempts: int = 3, dialect: str = "auto"):
        if dialect not in DIALECTS:
            raise ValueError(f"protocol must be one of {', '.join(DIALECTS)}")
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

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "simulated": isinstance(self.transport, SimulatedTransport),
            "address": getattr(self.transport, "address", None),
            "protocol": self.dialect or self.wanted_dialect,
            "state": self.state,
            "connected": self.transport.is_open,
            "last_error": self.last_error,
            "last_status": self.last_status,
            "last_used": self.last_used or None,
            "paper_used_mm": round(self.printed_dots * 25.4 / protocol.DPI),
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
        """Ask the printer (cat dialect) whether it can print before sending a job: out of paper or an open lid
        would otherwise swallow the job silently."""
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
        rows = raster.pack(bw)
        if self.dialect == "cat":
            return catproto.build_job(rows, bw.height, darkness, max(0, int(feed)) * LINE_DOTS)
        return protocol.build_job(rows, bw.height, darkness, feed)

    def print_image(self, bw: Image.Image, darkness=None, feed: int = 2, copies: int = 1,
                    cancel: threading.Event | None = None, progress=None) -> None:
        copies = max(1, min(20, int(copies)))
        with self.lock:
            self.connect()
            job = self._job(bw, darkness, feed)
            flow = catproto.flow_signal if self.dialect == "cat" else None
            try:
                self._ready_check()
                self.state = "printing"
                for n in range(copies):
                    def each(sent, total, n=n):
                        if progress:
                            progress((n * total + sent) / (copies * total))
                    self.transport.write(job, cancel, each, flow)
                    self._wait_done(bw.height)
                    self.printed_dots += bw.height + feed * LINE_DOTS
                self.state, self.last_error = "connected", None
            except Cancelled:
                self.state = "connected" if self.transport.is_open else "idle"
                raise
            except PrinterNotReady as exc:   # reachable and answering, just not able to print
                self.state, self.last_error = "connected" if self.transport.is_open else "idle", str(exc)
                raise
            except TransportError as exc:
                self.state, self.last_error = "error", str(exc)
                raise
            finally:
                self.last_used = time.monotonic()
                if self.transport.is_open:
                    self._schedule_close()

    def _wait_done(self, height: int) -> None:
        """Cat jobs end with a state request, answered once the printer reaches it: that's 'printed'."""
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
                if self.dialect == "cat":
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
                if self.dialect == "cat":
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


def make_printer(target: str, data_dir: str, channel: int = 1, keep_alive: float = 45.0,
                 dialect: str = "auto") -> Printer:
    """target is a Bluetooth address (AA:BB:CC:DD:EE:FF) or "sim"."""
    if not target:
        raise ValueError("no printer set: give its Bluetooth address, or 'sim' for the simulated printer")
    if target.lower() in ("sim", "simulated", "simulate"):
        import os
        sim_dialect = "escpos" if dialect == "escpos" else "cat"
        return Printer(SimulatedTransport(os.path.join(data_dir, "simulated-paper"), dialect=sim_dialect),
                       keep_alive=keep_alive, dialect=dialect)
    return Printer(BluetoothTransport(target, channel), keep_alive=keep_alive, dialect=dialect)
