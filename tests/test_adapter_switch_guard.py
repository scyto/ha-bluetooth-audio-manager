"""Switching the main adapter with clean=true needs the speaker count.

On 18 Nov 2026 a click on "Select" for a newly plugged-in second stick (meant
for one speaker) unpaired all three speakers and deleted their settings.  The
switch now has to be confirmed with the number of speakers it unpairs.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from bt_audio_manager.manager import BluetoothAudioManager
from bt_audio_manager.web.api import create_api_routes

NEW_STICK = "8A:88:4B:00:00:02"
SPEAKERS = [
    {"address": "41:42:00:00:00:01", "name": "87.5"},
    {"address": "41:42:00:00:00:02", "name": "ES-BT17"},
]


class UnpairCandidatesTest(unittest.TestCase):

    def test_union_of_managed_and_stored_with_names(self):
        m = BluetoothAudioManager.__new__(BluetoothAudioManager)
        m.managed_devices = {"41:42:00:00:00:01": object(), "41:42:00:00:00:03": object()}
        m.store = MagicMock()
        m.store.devices = [
            {"address": "41:42:00:00:00:01", "name": "ES-BT17"},
            {"address": "41:42:00:00:00:02", "name": "ES-BT17"},
        ]
        m.store.get_device.side_effect = lambda a: next(
            (d for d in m.store.devices if d["address"] == a), None
        )
        m.store.get_friendly_name.side_effect = lambda a: {"41:42:00:00:00:01": "87.5"}.get(a, "")
        self.assertEqual(m.unpair_candidates(), [
            {"address": "41:42:00:00:00:01", "name": "87.5"},
            {"address": "41:42:00:00:00:02", "name": "ES-BT17"},
            {"address": "41:42:00:00:00:03", "name": "41:42:00:00:00:03"},
        ])


class SwitchGuardApiTest(AioHTTPTestCase):

    async def get_application(self):
        self.manager = MagicMock()
        self.manager.unpair_candidates.return_value = list(SPEAKERS)
        self.manager.clear_all_devices = AsyncMock()
        self.manager.config.bt_adapter = "8A:88:4B:00:00:01"
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def test_impact_lists_speakers(self):
        resp = await self.client.get("/api/set-adapter/impact")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"count": 2, "speakers": SPEAKERS})

    async def test_clean_without_or_with_wrong_confirmation_changes_nothing(self):
        for extra in ({}, {"confirm_unpair": 3}, {"confirm_unpair": True}, {"confirm_unpair": "2"}):
            with self.subTest(extra=extra):
                resp = await self.client.post(
                    "/api/set-adapter", json={"adapter": NEW_STICK, "clean": True, **extra},
                )
                self.assertEqual(resp.status, 409)
                data = await resp.json()
                self.assertEqual(data["confirm_unpair_required"], 2)
                self.assertEqual(data["speakers"], SPEAKERS)
        self.manager.clear_all_devices.assert_not_awaited()
        self.manager.config.save_settings.assert_not_called()
        self.assertEqual(self.manager.config.bt_adapter, "8A:88:4B:00:00:01")

    async def test_clean_with_matching_count_switches(self):
        resp = await self.client.post(
            "/api/set-adapter", json={"adapter": NEW_STICK, "clean": True, "confirm_unpair": 2},
        )
        self.assertEqual(resp.status, 200)
        self.manager.clear_all_devices.assert_awaited_once()
        self.assertEqual(self.manager.config.bt_adapter, NEW_STICK)

    async def test_clean_with_no_speakers_needs_no_confirmation(self):
        self.manager.unpair_candidates.return_value = []
        resp = await self.client.post("/api/set-adapter", json={"adapter": NEW_STICK, "clean": True})
        self.assertEqual(resp.status, 200)

    async def test_switch_without_clean_is_unchanged(self):
        resp = await self.client.post("/api/set-adapter", json={"adapter": NEW_STICK})
        self.assertEqual(resp.status, 200)
        self.manager.clear_all_devices.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
