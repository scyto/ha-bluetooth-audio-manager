"""Read-only report of PulseAudio's Bluetooth bitrate recovery interval."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

try:
    from bt_audio_manager.audio.pulse import PulseAudioManager
    from bt_audio_manager.web.api import create_api_routes
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc


class ParseTest(unittest.TestCase):

    def test_interval_from_args(self):
        f = PulseAudioManager.rate_refresh_from_args
        self.assertIsNone(f(""))
        self.assertIsNone(f(None))
        self.assertEqual(f("output_rate_refresh_interval_ms=600000"), 600000)
        self.assertEqual(f("headset=native output_rate_refresh_interval_ms=5000"), 5000)
        self.assertIsNone(f("xoutput_rate_refresh_interval_ms=5"))


class DiagnosticsApiTest(AioHTTPTestCase):

    async def get_application(self):
        self.manager = MagicMock()
        pa = PulseAudioManager()
        pa._pulse = MagicMock()
        pa._pulse.module_list = AsyncMock(return_value=[
            SimpleNamespace(index=1, name="module-udev-detect", argument=""),
            SimpleNamespace(index=7, name="module-bluetooth-discover",
                            argument="output_rate_refresh_interval_ms=600000"),
            SimpleNamespace(index=8, name="module-bluez5-discover",
                            argument="output_rate_refresh_interval_ms=600000"),
        ])
        self.pa = pa
        self.manager.pulse = pa
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def test_reports_custom_interval(self):
        data = await (await self.client.get("/api/diagnostics/pulse-bluetooth")).json()
        self.assertEqual(data["output_rate_refresh_interval_ms"], 600000)
        self.assertTrue(data["custom"])
        self.assertEqual([m["name"] for m in data["modules"]],
                         ["module-bluetooth-discover", "module-bluez5-discover"])

    async def test_reports_default(self):
        self.pa._pulse.module_list.return_value = [
            SimpleNamespace(index=8, name="module-bluez5-discover", argument=None)]
        data = await (await self.client.get("/api/diagnostics/pulse-bluetooth")).json()
        self.assertIsNone(data["output_rate_refresh_interval_ms"])
        self.assertEqual(data["effective_interval_ms"], 500)
        self.assertFalse(data["custom"])

    async def test_no_pulse(self):
        self.manager.pulse = None
        resp = await self.client.get("/api/diagnostics/pulse-bluetooth")
        self.assertEqual(resp.status, 503)


if __name__ == "__main__":
    unittest.main()
