"""PUT /api/settings must act on Auto Reconnect, not just record it.

Cancelling in-flight reconnects is only useful if something actually calls it.
This covers the wiring between the settings endpoint and ReconnectService, so a
future refactor cannot quietly drop it and leave the toggle looking effective
while a reconnect finishes seizing the speaker (#281).
"""

import unittest
from unittest.mock import MagicMock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from bt_audio_manager.web.api import create_api_routes


def make_manager(*, auto_reconnect=True):
    mgr = MagicMock()
    mgr.config.auto_reconnect = auto_reconnect
    mgr.config.reconnect_interval_seconds = 30
    mgr.config.reconnect_max_backoff_seconds = 300
    mgr.config.scan_duration_seconds = 30
    mgr.config.runtime_settings = {
        "auto_reconnect": auto_reconnect,
        "reconnect_interval_seconds": 30,
        "reconnect_max_backoff_seconds": 300,
        "scan_duration_seconds": 30,
    }
    return mgr


class SettingsAutoReconnectTest(AioHTTPTestCase):

    async def get_application(self):
        self.manager = make_manager(auto_reconnect=True)
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def test_disabling_cancels_in_flight_reconnects(self):
        resp = await self.client.put("/api/settings", json={"auto_reconnect": False})
        self.assertEqual(resp.status, 200)
        self.manager.reconnect_service.cancel_all.assert_called_once()

    async def test_enabling_does_not_cancel(self):
        self.manager.config.auto_reconnect = False
        resp = await self.client.put("/api/settings", json={"auto_reconnect": True})
        self.assertEqual(resp.status, 200)
        self.manager.reconnect_service.cancel_all.assert_not_called()

    async def test_unrelated_setting_does_not_cancel(self):
        """Changing scan duration must not tear down active reconnects."""
        resp = await self.client.put("/api/settings", json={"scan_duration_seconds": 60})
        self.assertEqual(resp.status, 200)
        self.manager.reconnect_service.cancel_all.assert_not_called()

    async def test_already_off_does_not_cancel_again(self):
        """Only the on->off transition cancels, not every write while off."""
        self.manager.config.auto_reconnect = False
        resp = await self.client.put("/api/settings", json={"auto_reconnect": False})
        self.assertEqual(resp.status, 200)
        self.manager.reconnect_service.cancel_all.assert_not_called()

    async def test_survives_service_not_started(self):
        """reconnect_service is None until start(); this must not 500."""
        self.manager.reconnect_service = None
        resp = await self.client.put("/api/settings", json={"auto_reconnect": False})
        self.assertEqual(resp.status, 200)

    async def test_rejects_out_of_range_values(self):
        resp = await self.client.put(
            "/api/settings", json={"reconnect_interval_seconds": 1},
        )
        self.assertEqual(resp.status, 400)
        self.manager.reconnect_service.cancel_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
