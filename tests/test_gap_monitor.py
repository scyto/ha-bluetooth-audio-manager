"""Gap diagnosis: silence in the audio PulseAudio renders for a speaker."""

import asyncio
import struct
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from bt_audio_manager.audio.gap_monitor import (
    RATE, DeliveryStallDetector, GapMonitor, SilenceGapDetector,
)

try:
    from bt_audio_manager.web.api import create_api_routes
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc


def music(n):
    return struct.pack(f"<{n}h", *[(i % 50) + 100 for i in range(n)])


def silence(n):
    return b"\x00\x00" * n


def ms(n_ms):
    return RATE * n_ms // 1000


class DetectorTest(unittest.TestCase):

    def test_gap_inside_one_chunk(self):
        d = SilenceGapDetector()
        gaps = d.feed(music(ms(20)) + silence(ms(40)) + music(ms(40)), start_ts=100.0)
        self.assertEqual(len(gaps), 1)
        begin, dur = gaps[0]
        self.assertAlmostEqual(begin, 100.02, places=3)
        self.assertAlmostEqual(dur, 40, places=0)

    def test_short_silence_is_ignored(self):
        d = SilenceGapDetector()
        self.assertEqual(d.feed(music(ms(50)) + silence(ms(20)) + music(ms(30)), 0.0), [])

    def test_gap_spanning_chunks_is_reported_once(self):
        d = SilenceGapDetector()
        self.assertEqual(d.feed(music(ms(80)) + silence(ms(20)), 0.0), [])
        self.assertEqual(d.feed(silence(ms(100)), 0.1), [])
        gaps = d.feed(silence(ms(30)) + music(ms(70)), 0.2)
        self.assertEqual(len(gaps), 1)
        begin, dur = gaps[0]
        self.assertAlmostEqual(begin, 0.08, places=3)
        self.assertAlmostEqual(dur, 150, places=0)

    def test_open_gap_closes_when_next_chunk_starts_with_music(self):
        d = SilenceGapDetector()
        self.assertEqual(d.feed(music(ms(50)) + silence(ms(50)), 0.0), [])
        gaps = d.feed(music(ms(100)), 0.1)
        self.assertEqual([round(g[1]) for g in gaps], [50])

    def test_zero_low_byte_of_a_loud_sample_is_not_silence(self):
        # 0x0100 = 256: low byte zero, high byte not — must not merge into a run
        d = SilenceGapDetector()
        data = struct.pack("<h", 256) * ms(100)
        self.assertEqual(d.feed(data, 0.0), [])
        self.assertEqual(d.flush(), [])

    def test_flush_reports_trailing_gap(self):
        d = SilenceGapDetector()
        d.feed(music(ms(10)) + silence(ms(90)), 0.0)
        self.assertEqual([round(g[1]) for g in d.flush()], [90])


class StallDetectorTest(unittest.TestCase):
    """Rendering pauses show up as data arriving later than the samples say."""

    def steady(self, d, start, seconds, step=0.02):
        out, t = [], start
        for _ in range(round(seconds / step)):
            t += step
            out += d.feed(round(RATE * step), t)
        return out, t

    def test_steady_delivery_has_no_stall(self):
        d = DeliveryStallDetector()
        out, _ = self.steady(d, 0.0, 30)
        self.assertEqual(out, [])

    def test_jitter_below_threshold_is_ignored(self):
        d = DeliveryStallDetector()
        t, out = 0.0, []
        for i in range(1500):  # 30 s, arrival jitter of +-30 ms around the true time
            t_true = (i + 1) * 0.02
            out += d.feed(160, t_true + (0.03 if i % 7 == 0 else 0.0))
        self.assertEqual(out, [])

    def test_pause_then_catch_up_is_one_stall(self):
        d = DeliveryStallDetector()
        _, t = self.steady(d, 0.0, 5)
        # Bluetooth backs up: nothing arrives for 200 ms, then the backlog
        # arrives at once and delivery continues normally.
        t += 0.2
        out = d.feed(round(RATE * 0.2), t)
        rest, _ = self.steady(d, t, 3)
        stalls = out + rest
        self.assertEqual(len(stalls), 1)
        start, dur = stalls[0]
        self.assertAlmostEqual(start, 5.0, delta=0.03)
        self.assertAlmostEqual(dur, 180, delta=25)

    def test_pause_with_skip_recovers(self):
        # PulseAudio may skip instead of catching up gradually: more samples
        # than real time arrive afterwards; the stall must still be closed once.
        d = DeliveryStallDetector()
        _, t = self.steady(d, 0.0, 5)
        t += 0.3
        stalls = d.feed(round(RATE * 0.3), t)
        more, _ = self.steady(d, t, 2)
        self.assertEqual(len(stalls + more), 1)


class GapMonitorRunTest(unittest.IsolatedAsyncioTestCase):
    """End to end with a fake parec that pauses mid-stream."""

    async def test_fake_parec_stall_and_silence(self):
        import os
        import stat
        import tempfile
        from unittest.mock import patch

        script = (
            "#!/usr/bin/env python3\n"
            "import sys, struct, time\n"
            "out = sys.stdout.buffer\n"
            "mus = struct.pack('<160h', *[100 + i % 7 for i in range(160)])  # 20 ms\n"
            "sil = b'\\x00\\x00' * 400  # 50 ms\n"
            "def play(n):\n"
            "    for _ in range(n):\n"
            "        out.write(mus); out.flush(); time.sleep(0.02)\n"
            "play(40)\n"
            "time.sleep(0.25)           # output stall\n"
            "out.write(mus * 12); out.flush()  # catch-up burst\n"
            "play(30)\n"
            "out.write(sil); out.flush(); time.sleep(0.05)\n"
            "play(30)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "parec")
            with open(path, "w") as f:
                f.write(script)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
            with patch.dict(os.environ, {"PATH": tmp + os.pathsep + os.environ["PATH"]}):
                m = GapMonitor(["bluez_sink.A.a2dp_sink"], minutes=1)
                await m.start()
                for _ in range(100):
                    await asyncio.sleep(0.1)
                    if m.errors:
                        break
                await m.stop()
        kinds = [e["kind"] for e in m.events]
        self.assertIn("silence", kinds)
        self.assertEqual(kinds.count("stall"), 1)
        stall = next(e for e in m.events if e["kind"] == "stall")
        self.assertGreater(stall["duration_ms"], 150)
        self.assertEqual(len(m.stall_clusters()), 1)


class GapMonitorApiTest(AioHTTPTestCase):

    async def get_application(self):
        self.manager = MagicMock()
        self.manager.start_gap_monitor = AsyncMock(return_value={"running": True})
        self.manager.stop_gap_monitor = AsyncMock(return_value={"running": False})
        self.manager.gap_monitor_status.return_value = None
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def test_start_with_minutes(self):
        resp = await self.client.post("/api/diagnostics/gap-monitor", json={"minutes": 5})
        self.assertEqual(resp.status, 200)
        self.manager.start_gap_monitor.assert_awaited_once_with(5)

    async def test_start_defaults_to_ten_minutes(self):
        resp = await self.client.post("/api/diagnostics/gap-monitor")
        self.assertEqual(resp.status, 200)
        self.manager.start_gap_monitor.assert_awaited_once_with(10)

    async def test_bad_minutes_rejected(self):
        for value in (0, -1, "5", True):
            with self.subTest(value=value):
                resp = await self.client.post("/api/diagnostics/gap-monitor", json={"minutes": value})
                self.assertEqual(resp.status, 400)
        self.manager.start_gap_monitor.assert_not_called()

    async def test_no_sink_is_409(self):
        self.manager.start_gap_monitor.side_effect = RuntimeError("no Bluetooth A2DP sink present")
        resp = await self.client.post("/api/diagnostics/gap-monitor", json={"minutes": 1})
        self.assertEqual(resp.status, 409)

    async def test_status_404_before_first_run(self):
        resp = await self.client.get("/api/diagnostics/gap-monitor")
        self.assertEqual(resp.status, 404)

    async def test_stop(self):
        resp = await self.client.delete("/api/diagnostics/gap-monitor")
        self.assertEqual(resp.status, 200)
        self.manager.stop_gap_monitor.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
