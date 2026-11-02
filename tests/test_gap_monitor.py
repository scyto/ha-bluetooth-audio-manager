"""Gap diagnosis: silence in the audio PulseAudio renders for a speaker."""

import struct
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from bt_audio_manager.audio.gap_monitor import RATE, SilenceGapDetector

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
