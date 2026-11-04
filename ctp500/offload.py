"""Heavy jobs handed to the desktop PC while its helper runs (desktop/worker.py, 2026-11-02).

The Pi stays the hub (gallery, queue, Bluetooth to the printer, always on). The PC's helper long-polls
GET /api/helper/next with the kinds of job it can do; the Pi gives it the oldest matching job and the caller waits for
POST /api/helper/result/<id>. The PC calls out to the Pi, so Windows needs no inbound firewall rule. A kind counts as
available while some poller asked for it in the last ONLINE_S seconds; a caller whose job isn't answered in time gets
Unavailable and does the work itself (or, for PC-only work like slicing, says to turn the PC on).
"""

from __future__ import annotations

import collections
import itertools
import threading
import time


class Unavailable(Exception):
    """The PC helper isn't running (or didn't answer in time)."""


class Offload:
    ONLINE_S = 40.0            # a poll waits up to 25 s, so a running helper is never older than that between polls

    def __init__(self):
        self._cv = threading.Condition()
        self._queue: collections.deque = collections.deque()
        self._pending: dict = {}           # job id -> (Event, result box)
        self._ids = itertools.count(1)
        self._seen: dict[str, float] = {}  # kind -> monotonic time of the last poll that could take it
        self.name = ""

    def online(self, kind: str | None = None) -> bool:
        now = time.monotonic()
        with self._cv:
            if kind is None:
                return any(now - t < self.ONLINE_S for t in self._seen.values())
            return now - self._seen.get(kind, -1e9) < self.ONLINE_S

    def status(self) -> dict:
        now = time.monotonic()
        with self._cv:
            kinds = sorted(k for k, t in self._seen.items() if now - t < self.ONLINE_S)
        return {"online": bool(kinds), "name": self.name if kinds else "", "kinds": kinds}

    def run(self, kind: str, data: bytes, params: dict | None = None, timeout: float = 60.0) -> tuple[bytes, dict]:
        """(result bytes, meta) from the PC, or Unavailable."""
        if not self.online(kind):
            raise Unavailable(f"the PC helper isn't running ({kind})")
        job = {"id": str(next(self._ids)), "kind": kind, "params": params or {}, "data": data}
        done, box = threading.Event(), {}
        with self._cv:
            self._pending[job["id"]] = (done, box)
            self._queue.append(job)
            self._cv.notify_all()
        if not done.wait(timeout):
            with self._cv:
                self._pending.pop(job["id"], None)
                if job in self._queue:
                    self._queue.remove(job)
            raise Unavailable(f"the PC didn't answer in {timeout:g} s ({kind})")
        if box.get("error"):
            raise RuntimeError(f"the PC: {box['error']}")
        return box["data"], box.get("meta") or {}

    def next(self, kinds: list[str], name: str = "", wait: float = 25.0) -> dict | None:
        """The helper's long poll: the oldest job it can do, or None after `wait` seconds."""
        deadline = time.monotonic() + wait
        with self._cv:
            self.name = name or self.name
            while True:
                now = time.monotonic()
                for k in kinds:
                    self._seen[k] = now
                for job in self._queue:
                    if job["kind"] in kinds:
                        self._queue.remove(job)
                        return job
                left = deadline - now
                if left <= 0:
                    return None
                self._cv.wait(min(left, 5.0))

    def result(self, job_id: str, data: bytes = b"", meta: dict | None = None, error: str | None = None) -> bool:
        with self._cv:
            entry = self._pending.pop(job_id, None)
        if not entry:
            return False                   # the caller gave up waiting
        done, box = entry
        box.update(data=data, meta=meta or {}, error=error)
        done.set()
        return True
