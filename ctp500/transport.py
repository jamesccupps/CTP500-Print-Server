"""Getting bytes to the printer: a Bluetooth RFCOMM socket, or a simulated printer that saves PNGs."""

from __future__ import annotations

import os
import socket
import threading
import time

from . import catproto, protocol, simulate


class TransportError(OSError):
    pass


class Cancelled(Exception):
    pass


class BluetoothTransport:
    """Bluetooth Classic SPP over a plain RFCOMM socket (works on Linux/BlueZ and Windows)."""

    def __init__(self, address: str, channel: int = 1, timeout: float = 12.0,
                 chunk_size: int = protocol.CHUNK_SIZE, chunk_delay: float = protocol.CHUNK_DELAY):
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise TransportError("this Python has no Bluetooth socket support")
        self.address = address.upper()
        self.channel = int(channel)
        self.timeout = timeout
        self.chunk_size = max(16, int(chunk_size))
        self.chunk_delay = max(0.0, float(chunk_delay))
        self._sock: socket.socket | None = None
        self._last_write = 0.0

    @property
    def name(self) -> str:
        return f"bluetooth {self.address}"

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        if self._sock:
            return
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((self.address, self.channel))
        except OSError as exc:
            sock.close()
            raise TransportError(_explain(exc, self.address)) from None
        self._sock = sock

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock:
            # Closing right after a send can drop what's still in flight; give the link a moment.
            wait = 1.5 - (time.monotonic() - self._last_write)
            if wait > 0:
                time.sleep(wait)
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def write(self, data: bytes, cancel: threading.Event | None = None, progress=None, flow=None) -> None:
        """Send in paced chunks. flow(incoming) -> True (pause) / False (resume) / None lets the printer
        hold us off while its buffer is full."""
        if not self._sock:
            raise TransportError("not connected")
        total, sent = len(data), 0
        paused_since = None
        try:
            while sent < total:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if flow is not None:
                    signal = flow(self._poll())
                    if signal is not None:
                        paused_since = time.monotonic() if signal else None
                    if paused_since is not None:
                        if time.monotonic() - paused_since > 20:
                            raise TransportError("the printer stopped taking data (out of paper or lid open?)")
                        time.sleep(0.02)
                        continue
                chunk = data[sent:sent + self.chunk_size]
                self._sock.sendall(chunk)
                sent += len(chunk)
                if progress:
                    progress(sent, total)
                if self.chunk_delay:
                    time.sleep(self.chunk_delay)
            self._last_write = time.monotonic()
        except OSError as exc:
            self.close()
            raise TransportError(f"lost the printer while sending ({exc})") from None

    def _poll(self) -> bytes:
        """Whatever the printer has sent, without waiting."""
        self._sock.settimeout(0.0)
        out = bytearray()
        try:
            while True:
                chunk = self._sock.recv(256)
                if not chunk:
                    break
                out += chunk
        except (BlockingIOError, socket.timeout, InterruptedError):
            pass
        finally:
            self._sock.settimeout(self.timeout)
        return bytes(out)

    def read_until(self, done, timeout: float) -> bytes:
        """Collect incoming bytes until done(collected) is true or the time runs out."""
        if not self._sock:
            raise TransportError("not connected")
        got = bytearray()
        deadline = time.monotonic() + timeout
        try:
            self._sock.settimeout(0.25)
            while time.monotonic() < deadline and not done(bytes(got)):
                try:
                    chunk = self._sock.recv(256)
                except socket.timeout:
                    continue
                if not chunk:
                    raise TransportError("printer closed the connection")
                got += chunk
            return bytes(got)
        except OSError as exc:
            self.close()
            raise TransportError(f"lost the printer ({exc})") from None
        finally:
            if self._sock:
                self._sock.settimeout(self.timeout)

    def request(self, command: bytes, timeout: float = 1.5) -> bytes:
        """Send a command and collect whatever comes back until the line goes quiet."""
        if not self._sock:
            raise TransportError("not connected")
        self._drain()
        try:
            self._sock.sendall(command)
            reply = bytearray()
            deadline = time.monotonic() + timeout
            self._sock.settimeout(timeout)
            while time.monotonic() < deadline:
                try:
                    chunk = self._sock.recv(256)
                except socket.timeout:
                    break
                if not chunk:
                    raise TransportError("printer closed the connection")
                reply += chunk
                self._sock.settimeout(0.25)  # more may follow; stop at the first quiet gap
            return bytes(reply)
        except OSError as exc:
            self.close()
            raise TransportError(f"lost the printer ({exc})") from None
        finally:
            if self._sock:
                self._sock.settimeout(self.timeout)

    def _drain(self) -> None:
        self._sock.settimeout(0.0)
        try:
            while self._sock.recv(256):
                pass
        except (BlockingIOError, socket.timeout, InterruptedError):
            pass
        except OSError:
            pass
        finally:
            self._sock.settimeout(self.timeout)


def _explain(exc: OSError, address: str) -> str:
    text = str(exc)
    hints = {
        111: "the printer refused the connection; is it already connected to a phone?",
        112: "the printer is off or out of range",
        113: "the printer is off or out of range",
        110: "timed out; is the printer on and nearby?",
        16: "Bluetooth is busy with that device; try again in a moment",
        10060: "timed out; is the printer on and nearby?",
        10064: "the printer is off or out of range",
        10050: "Bluetooth is off on this computer",
    }
    hint = hints.get(getattr(exc, "errno", None) or getattr(exc, "winerror", None))
    if isinstance(exc, socket.timeout):
        hint = "timed out; is the printer on and nearby?"
    return f"couldn't connect to {address}: {hint or text}"


class SimulatedTransport:
    """Pretends to be the printer: decodes each job back into paper and saves it as a PNG."""

    def __init__(self, out_dir: str, keep: int = 50, dialect: str = "cat"):
        self.out_dir = out_dir
        self.keep = keep
        self.dialect = dialect      # which protocol it answers to when probed, like the real one
        self._open = False
        self._buf = bytearray()
        self._replies = bytearray()
        self.last_paper = None
        self.last_info: dict | None = None
        self.state_flags = 0        # what A3 answers (tests set 0x01 for "out of paper")
        os.makedirs(out_dir, exist_ok=True)

    name = "simulated printer"
    INFO_REPLY = catproto.packet(catproto.DEVICE_INFO, b"SIMULATED")

    @property
    def STATE_REPLY(self) -> bytes:
        return catproto.packet(catproto.DEVICE_STATE, bytes([self.state_flags, 0x0B, 0x28]))

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._flush()
        self._open = False

    def write(self, data: bytes, cancel: threading.Event | None = None, progress=None, flow=None) -> None:
        if not self._open:
            raise TransportError("not connected")
        for start in range(0, len(data), 4096):
            if cancel is not None and cancel.is_set():
                self._buf.clear()
                raise Cancelled()
            self._buf += data[start:start + 4096]
            if progress:
                progress(min(len(data), start + 4096), len(data))
        self._replies += self._answer(data)
        self._flush()

    def _answer(self, data: bytes) -> bytes:
        if self.dialect != "cat":
            return b""
        out = bytearray()
        for cmd, _ in catproto.parse_packets(data):
            if cmd == catproto.DEVICE_STATE:
                out += self.STATE_REPLY
            elif cmd == catproto.DEVICE_INFO:
                out += self.INFO_REPLY
        return bytes(out)

    def request(self, command: bytes, timeout: float = 1.5) -> bytes:
        if not self._open:
            raise TransportError("not connected")
        reply = self._answer(command)
        if command.startswith(catproto.PREFIX):
            return reply
        return b"" if self.dialect == "cat" else b"SIMULATED\x00"

    def read_until(self, done, timeout: float) -> bytes:
        data, self._replies = bytes(self._replies), bytearray()
        return data

    def _flush(self) -> None:
        if not self._buf:
            return
        data, self._buf = bytes(self._buf), bytearray()
        paper, info = simulate.decode(data)
        self.last_info = info
        if paper is None:
            return
        self.last_paper = paper
        stamp = time.strftime("%Y%m%d-%H%M%S")
        paper.save(os.path.join(self.out_dir, f"paper-{stamp}-{int(time.time() * 1000) % 1000:03d}.png"))
        files = sorted(f for f in os.listdir(self.out_dir) if f.startswith("paper-"))
        for old in files[:-self.keep]:
            try:
                os.remove(os.path.join(self.out_dir, old))
            except OSError:
                pass
