"""Read-only access to the PulseAudio (audio plugin) log.

PulseAudio logs "Skipping N us (= M bytes) in audio stream" when it has to
drop audio because the Bluetooth socket did not take data in time.  That
log lives in HAOS's audio plugin, reachable only through the Supervisor.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

try:
    from bt_audio_manager.web.api import create_api_routes
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

LOG = [
    "2026-11-02 21:30:01.000 pulseaudio[1]: W: [bluetooth] module-bluez5-device.c: Skipping 120000 us (= 21168 bytes) in audio stream",
    "2026-11-02 21:30:05.000 pulseaudio[1]: I: [pulseaudio] client.c: Created 12 \"Native client\"",
]


class AudioLogsApiTest(AioHTTPTestCase):

    async def get_application(self):
        self.manager = MagicMock()
        self.manager.fetch_audio_logs = AsyncMock(return_value=list(LOG))
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def test_grep_filters_case_insensitive(self):
        data = await (await self.client.get("/api/diagnostics/audio-logs?grep=skipping")).json()
        self.assertEqual(data["fetched"], 2)
        self.assertEqual(data["count"], 1)
        self.assertIn("Skipping 120000 us", data["lines"][0])

    async def test_defaults_ask_for_timestamps(self):
        await self.client.get("/api/diagnostics/audio-logs")
        self.manager.fetch_audio_logs.assert_awaited_once_with(500, timestamps=True)

    async def test_bad_lines_rejected(self):
        for q in ("lines=abc", "lines=0"):
            with self.subTest(q=q):
                resp = await self.client.get(f"/api/diagnostics/audio-logs?{q}")
                self.assertEqual(resp.status, 400)

    async def test_no_supervisor_is_503(self):
        self.manager.fetch_audio_logs.return_value = None
        resp = await self.client.get("/api/diagnostics/audio-logs")
        self.assertEqual(resp.status, 503)


if __name__ == "__main__":
    unittest.main()
