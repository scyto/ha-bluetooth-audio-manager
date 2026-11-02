"""Temporary diagnostic: where does a speaker dropout happen?

Records each Bluetooth sink's *monitor* source (what PulseAudio renders for
that sink) at 8 kHz mono and looks for two different things:

* **Silence** — a stretch of exact digital silence of at least MIN_GAP_MS.
  When a player stream underruns (MPD, network) PulseAudio renders silence,
  so the audio is already missing *before* Bluetooth.  Radio streams
  practically never contain 30 ms of exact zeros in music; a station's own
  silence between items can.

* **Stall** — PulseAudio's bluez5 sink only renders when the Bluetooth
  socket takes data.  When the link, the adapter or the USB path backs up,
  rendering pauses and the monitor falls behind the wall clock; once the
  link frees up PulseAudio catches up (and skips), and the lag returns.
  A rise of that lag above its recent minimum is reported as a stall.
  Stalls on all speakers at the same moment point at the shared path
  (adapter, USB, host); stalls on one speaker at a time point at that
  link (radio path, speaker).

An audible dropout with neither happened inside the speaker or on the
radio without backing up the adapter.

Each monitor is read by a plain thread so a busy event loop cannot fake a
stall.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

RATE = 8000  # Hz, mono s16le
MIN_GAP_MS = 30
STALL_MS = 60
_ZERO_BYTES = re.compile(rb"\x00+")


class DeliveryStallDetector:
    """Pauses in rendering, from monitor data arrival times vs. sample count."""

    def __init__(self, rate: int = RATE, threshold_ms: float = STALL_MS, window_s: float = 10.0):
        self.rate = rate
        self.threshold = threshold_ms / 1000
        self.window = window_s
        self._t0: float | None = None
        self._samples = 0
        self._history: collections.deque = collections.deque()  # (t, lag)
        self._stall_start: float | None = None
        self._peak = 0.0

    def feed(self, n_samples: int, now: float) -> list[tuple[float, float]]:
        """Data for *n_samples* arrived at monotonic time *now*.

        Returns finished stalls as ``(start_monotonic, duration_ms)``.
        """
        if self._t0 is None:
            self._t0 = now - n_samples / self.rate
        # How late the first sample of this chunk arrived, measured against
        # the sample clock.  Healthy: about one chunk.  After a pause: the
        # whole pause, even though the backlog then arrives in one go.
        lag = (now - self._t0) - self._samples / self.rate
        self._samples += n_samples
        while self._history and now - self._history[0][0] > self.window:
            self._history.popleft()
        baseline = min((l for _, l in self._history), default=lag)
        self._history.append((now, lag))
        excess = lag - baseline
        done: list[tuple[float, float]] = []
        if excess > self.threshold:
            if self._stall_start is None:
                # Data stopped arriving roughly `excess` ago.
                self._stall_start, self._peak = now - excess, excess
            else:
                self._peak = max(self._peak, excess)
        elif self._stall_start is not None and excess < self.threshold / 2:
            done.append((self._stall_start, self._peak * 1000))
            self._stall_start = None
        return done

class SilenceGapDetector:
    """Finds runs of zero samples in a s16le mono byte stream fed in chunks."""

    def __init__(self, rate: int = RATE, min_gap_ms: int = MIN_GAP_MS):
        self.rate = rate
        self.min_samples = max(1, rate * min_gap_ms // 1000)
        self._open_samples = 0
        self._open_start = 0.0

    def feed(self, data: bytes, start_ts: float) -> list[tuple[float, float]]:
        """Feed one sample-aligned chunk; *start_ts* = time of its first sample.

        Returns closed gaps as ``(start_ts, duration_ms)``.
        """
        gaps: list[tuple[float, float]] = []
        n_samples = len(data) // 2
        if data[:2] != b"\x00\x00":
            self._close(gaps)
        for match in _ZERO_BYTES.finditer(data, 0, n_samples * 2):
            s, e = match.span()
            first, last = (s + 1) // 2, e // 2  # whole zero samples [first, last)
            if last <= first:
                continue
            count = last - first
            if first == 0 and self._open_samples:
                count += self._open_samples
                begin = self._open_start
            else:
                self._close(gaps)
                begin = start_ts + first / self.rate
            if last == n_samples:  # may continue in the next chunk
                self._open_samples, self._open_start = count, begin
            else:
                self._open_samples = 0
                self._emit(gaps, begin, count)
        return gaps

    def flush(self) -> list[tuple[float, float]]:
        gaps: list[tuple[float, float]] = []
        self._close(gaps)
        return gaps

    def _close(self, gaps: list) -> None:
        if self._open_samples:
            self._emit(gaps, self._open_start, self._open_samples)
            self._open_samples = 0

    def _emit(self, gaps: list, begin: float, count: int) -> None:
        if count >= self.min_samples:
            gaps.append((begin, count * 1000 / self.rate))


class GapMonitor:
    """Runs one ``parec`` per sink monitor for a limited time and collects events."""

    MAX_EVENTS = 1000
    CLUSTER_S = 0.5  # stalls on several speakers within this window = one event

    def __init__(self, sinks: list[str], minutes: float):
        self.sinks = list(sinks)
        self.minutes = minutes
        self.started_at = time.time()
        self.ends_at = self.started_at + minutes * 60
        self.events: collections.deque = collections.deque(maxlen=self.MAX_EVENTS)
        self.bytes_read: dict[str, int] = {s: 0 for s in self.sinks}
        self.errors: dict[str, str] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    async def start(self) -> None:
        for sink in self.sinks:
            self._tasks.append(asyncio.create_task(asyncio.to_thread(self._read_blocking, sink)))
        self._tasks.append(asyncio.create_task(self._stop_later()))
        logger.warning(
            "Gap monitor started for %g min on %s", self.minutes, ", ".join(self.sinks),
        )

    async def stop(self) -> None:
        self._stopping = True
        for proc in list(self._procs.values()):
            if proc.poll() is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    task.cancel()

    async def _stop_later(self) -> None:
        await asyncio.sleep(max(0.0, self.ends_at - time.time()))
        await self.stop()
        logger.warning("Gap monitor finished: %s", self.summary())

    def summary(self) -> dict:
        with self._lock:
            events = list(self.events)
        out = {}
        for sink in self.sinks:
            mine = [e for e in events if e["sink"] == sink]
            out[sink] = {
                "silence": sum(e["kind"] == "silence" for e in mine),
                "stall": sum(e["kind"] == "stall" for e in mine),
            }
        return out

    def stall_clusters(self) -> list[dict]:
        """Stalls grouped across speakers: same moment = shared cause."""
        with self._lock:
            stalls = sorted((e for e in self.events if e["kind"] == "stall"), key=lambda e: e["ts"])
        clusters: list[dict] = []
        for e in stalls:
            if clusters and e["ts"] - clusters[-1]["ts"] <= self.CLUSTER_S:
                c = clusters[-1]
                c["sinks"] = sorted(set(c["sinks"]) | {e["sink"]})
                c["max_ms"] = max(c["max_ms"], e["duration_ms"])
            else:
                clusters.append({"at": e["at"], "ts": e["ts"], "sinks": [e["sink"]], "max_ms": e["duration_ms"]})
        for prev, cur in zip(clusters, clusters[1:]):
            cur["since_previous_s"] = round(cur["ts"] - prev["ts"], 1)
        return clusters

    def status(self) -> dict:
        with self._lock:
            events = list(self.events)
            seconds = {s: round(b / 2 / RATE, 1) for s, b in self.bytes_read.items()}
        return {
            "running": self.running,
            "started_at": self.started_at,
            "ends_at": self.ends_at,
            "min_gap_ms": MIN_GAP_MS,
            "stall_threshold_ms": STALL_MS,
            "seconds_recorded": seconds,
            "events_per_sink": self.summary(),
            "stall_clusters": self.stall_clusters(),
            "errors": dict(self.errors),
            "events": events,
        }

    def _read_blocking(self, sink: str) -> None:
        silence = SilenceGapDetector()
        stalls = DeliveryStallDetector()
        try:
            proc = subprocess.Popen(
                ["parec", f"--device={sink}.monitor",
                 "--format=s16le", f"--rate={RATE}", "--channels=1", "--raw",
                 "--latency-msec=20", "--client-name=bt-audio-manager-gap-monitor"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError) as e:
            self.errors[sink] = str(e)
            return
        self._procs[sink] = proc
        fd = proc.stdout.fileno()
        pending = b""
        try:
            while True:
                data = os.read(fd, 4096)
                mono, wall = time.monotonic(), time.time()
                if not data:
                    break
                pending += data
                usable = len(pending) - len(pending) % 2
                chunk, pending = pending[:usable], pending[usable:]
                if not chunk:
                    continue
                n = len(chunk) // 2
                with self._lock:
                    self.bytes_read[sink] += len(chunk)
                for begin, dur in silence.feed(chunk, wall - n / RATE):
                    self._record("silence", sink, begin, dur)
                for begin_mono, dur in stalls.feed(n, mono):
                    self._record("stall", sink, wall - (mono - begin_mono), dur)
        finally:
            for begin, dur in silence.flush():
                self._record("silence", sink, begin, dur)
            if proc.poll() is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            proc.stdout.close()
            if not self._stopping and sink not in self.errors:
                self.errors[sink] = "recording ended early (sink gone?)"

    def _record(self, kind: str, sink: str, begin: float, duration_ms: float) -> None:
        event = {
            "kind": kind,
            "sink": sink,
            "at": time.strftime("%H:%M:%S", time.localtime(begin)) + f".{int(begin % 1 * 10)}",
            "ts": round(begin, 2),
            "duration_ms": round(duration_ms),
        }
        with self._lock:
            self.events.append(event)
        if kind == "silence":
            logger.warning(
                "Audio gap before Bluetooth: %d ms of silence on %s at %s",
                event["duration_ms"], sink, event["at"],
            )
        else:
            logger.warning(
                "Bluetooth output stalled: %d ms on %s at %s",
                event["duration_ms"], sink, event["at"],
            )
