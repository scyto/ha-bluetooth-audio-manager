"""Temporary diagnostic: find gaps in the audio PulseAudio sends to a speaker.

Records each Bluetooth sink's *monitor* source (what PulseAudio renders for
that sink) at 8 kHz mono and reports every stretch of exact digital silence
of at least MIN_GAP_MS.

Why this splits the problem in two: when a player stream underruns (MPD,
network, PulseAudio scheduling), PulseAudio renders silence, which shows up
here as zeros.  When instead the Bluetooth link stalls, PulseAudio's
bluez5 sink still renders the audio it has to skip, so the monitor stays
continuous.  An audible dropout with no gap here therefore happened after
PulseAudio: radio, controller or speaker.

Radio streams practically never contain 30 ms of exact zeros in music;
a station's own silence between items can, so isolated hits next to a
title change are expected.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
import time

logger = logging.getLogger(__name__)

RATE = 8000  # Hz, mono s16le
CHUNK_BYTES = 1600  # 100 ms
MIN_GAP_MS = 30
_ZERO_BYTES = re.compile(rb"\x00+")


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
    """Runs one ``parec`` per sink monitor for a limited time and collects gaps."""

    MAX_EVENTS = 500

    def __init__(self, sinks: list[str], minutes: float):
        self.sinks = list(sinks)
        self.minutes = minutes
        self.started_at = time.time()
        self.ends_at = self.started_at + minutes * 60
        self.events: collections.deque = collections.deque(maxlen=self.MAX_EVENTS)
        self.bytes_read: dict[str, int] = {s: 0 for s in self.sinks}
        self.errors: dict[str, str] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    async def start(self) -> None:
        for sink in self.sinks:
            self._tasks.append(asyncio.create_task(self._run(sink)))
        self._tasks.append(asyncio.create_task(self._stop_later()))
        logger.warning(
            "Gap monitor started for %g min on %s", self.minutes, ", ".join(self.sinks),
        )

    async def stop(self) -> None:
        self._stopping = True
        for proc in self._procs.values():
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current and not task.done():
                task.cancel()
        for task in self._tasks:
            if task is not current:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _stop_later(self) -> None:
        await asyncio.sleep(max(0.0, self.ends_at - time.time()))
        await self.stop()
        logger.warning("Gap monitor finished: %s", self.summary())

    def summary(self) -> dict:
        per_sink = collections.Counter(e["sink"] for e in self.events)
        return {s: per_sink.get(s, 0) for s in self.sinks}

    def status(self) -> dict:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "ends_at": self.ends_at,
            "min_gap_ms": MIN_GAP_MS,
            "seconds_recorded": {s: round(b / 2 / RATE, 1) for s, b in self.bytes_read.items()},
            "gaps_per_sink": self.summary(),
            "errors": dict(self.errors),
            "events": list(self.events),
        }

    async def _run(self, sink: str) -> None:
        detector = SilenceGapDetector()
        try:
            proc = await asyncio.create_subprocess_exec(
                "parec", f"--device={sink}.monitor",
                "--format=s16le", f"--rate={RATE}", "--channels=1", "--raw",
                "--latency-msec=200", "--client-name=bt-audio-manager-gap-monitor",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError) as e:
            self.errors[sink] = str(e)
            return
        self._procs[sink] = proc
        try:
            while True:
                try:
                    data = await proc.stdout.readexactly(CHUNK_BYTES)
                except asyncio.IncompleteReadError:
                    break
                self.bytes_read[sink] += len(data)
                # Best estimate of when the chunk's first sample was rendered.
                start_ts = time.time() - (len(data) // 2) / RATE
                for begin, dur in detector.feed(data, start_ts):
                    self._record(sink, begin, dur)
        finally:
            for begin, dur in detector.flush():
                self._record(sink, begin, dur)
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            if not self._stopping and sink not in self.errors:
                self.errors[sink] = "recording ended early (sink gone?)"

    def _record(self, sink: str, begin: float, duration_ms: float) -> None:
        event = {
            "sink": sink,
            "at": time.strftime("%H:%M:%S", time.localtime(begin)) + f".{int(begin % 1 * 10)}",
            "ts": round(begin, 2),
            "duration_ms": round(duration_ms),
        }
        self.events.append(event)
        logger.warning(
            "Audio gap before Bluetooth: %d ms of silence on %s at %s",
            event["duration_ms"], sink, event["at"],
        )
