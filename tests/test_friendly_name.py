"""Friendly names: the user's own name for a speaker.

Several identical speakers report the same Bluetooth name (three "ES-BT17"),
so the UI could only tell them apart by MAC.  A friendly name is stored per
address, apart from the device record, so it survives Forget + re-pair.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from bt_audio_manager.persistence.store import FRIENDLY_NAME_MAX_LEN, PersistenceStore
from bt_audio_manager.web.api import create_api_routes

try:
    from bt_audio_manager.manager import BluetoothAudioManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    BluetoothAudioManager = None
    _MANAGER_SKIP = f"libpulse not available: {exc}"
else:
    _MANAGER_SKIP = None

A = "41:42:E8:96:EE:34"
B = "41:42:86:95:15:12"


class StoreFriendlyNameTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "paired_devices.json"
        self.store = PersistenceStore(str(self.path))
        await self.store.load()
        await self.store.add_device(A, "ES-BT17")

    async def reload(self):
        store = PersistenceStore(str(self.path))
        await store.load()
        return store

    async def test_set_persists_and_reloads(self):
        self.assertEqual(await self.store.set_friendly_name(A, "  UKW 87.5 "), "UKW 87.5")
        self.assertEqual(self.store.get_friendly_name(A), "UKW 87.5")
        self.assertEqual((await self.reload()).get_friendly_name(A), "UKW 87.5")

    async def test_lookup_ignores_address_case(self):
        await self.store.set_friendly_name(A.lower(), "UKW 87.5")
        self.assertEqual(self.store.get_friendly_name(A), "UKW 87.5")

    async def test_empty_name_clears_it(self):
        await self.store.set_friendly_name(A, "UKW 87.5")
        self.assertEqual(await self.store.set_friendly_name(A, "   "), "")
        self.assertEqual(self.store.get_friendly_name(A), "")
        data = json.loads(self.path.read_text())
        self.assertNotIn("friendly_names", data)

    async def test_survives_forget_and_repair(self):
        await self.store.set_friendly_name(A, "UKW 87.5")
        await self.store.remove_device(A)
        await self.store.add_device(A, "ES-BT17")
        self.assertEqual((await self.reload()).get_friendly_name(A), "UKW 87.5")

    async def test_repairing_does_not_overwrite_it(self):
        """add_device() rewrites the stored Bluetooth name, never the friendly one."""
        await self.store.set_friendly_name(A, "UKW 87.5")
        await self.store.add_device(A, "ES-BT17 v2")
        self.assertEqual(self.store.get_device(A)["name"], "ES-BT17 v2")
        self.assertEqual(self.store.get_friendly_name(A), "UKW 87.5")

    async def test_clear_all_drops_names(self):
        await self.store.set_friendly_name(A, "UKW 87.5")
        await self.store.clear_all()
        self.assertEqual((await self.reload()).get_friendly_name(A), "")

    async def test_unusable_entries_in_file_are_skipped(self):
        self.path.write_text(json.dumps({
            "devices": [],
            "friendly_names": {A.lower(): " UKW 87.5 ", B: 42, "x": "", 7: "y"},
        }))
        store = await self.reload()
        self.assertEqual(store.get_friendly_name(A), "UKW 87.5")
        self.assertEqual(store.get_friendly_name(B), "")

    async def test_old_store_without_names_loads(self):
        self.path.write_text(json.dumps({"devices": [{"address": A, "name": "ES-BT17"}]}))
        store = await self.reload()
        self.assertEqual(store.get_friendly_name(A), "")
        self.assertEqual(len(store.devices), 1)


def make_manager(*, stored=True):
    mgr = MagicMock()
    mgr.store.get_device.return_value = {"address": A, "name": "ES-BT17"} if stored else None
    mgr.store.get_device_settings.return_value = {"mpd_enabled": True, "mpd_port": 6600}
    names = {}
    mgr.store.get_friendly_name.side_effect = lambda addr: names.get(addr, "")

    async def set_name(addr, name):
        names[addr] = name
        return name

    mgr.set_friendly_name = AsyncMock(side_effect=set_name)
    mgr.update_device_settings = AsyncMock(return_value={"address": A})
    return mgr


class FriendlyNameApiTest(AioHTTPTestCase):

    async def get_application(self):
        self.manager = make_manager()
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def put(self, body):
        return await self.client.put(f"/api/devices/{A}/settings", json=body)

    async def test_name_alone_is_accepted_and_trimmed(self):
        resp = await self.put({"friendly_name": "  UKW 87.5  "})
        self.assertEqual(resp.status, 200)
        self.manager.set_friendly_name.assert_awaited_once_with(A, "UKW 87.5")
        self.manager.update_device_settings.assert_not_called()
        data = await resp.json()
        self.assertEqual(data["settings"]["friendly_name"], "UKW 87.5")

    async def test_name_with_other_settings_updates_both(self):
        resp = await self.put({"friendly_name": "UKW 87.5", "volume_lock": True})
        self.assertEqual(resp.status, 200)
        self.manager.update_device_settings.assert_awaited_once_with(A, {"volume_lock": True})
        self.manager.set_friendly_name.assert_awaited_once_with(A, "UKW 87.5")

    async def test_empty_string_clears(self):
        resp = await self.put({"friendly_name": ""})
        self.assertEqual(resp.status, 200)
        self.manager.set_friendly_name.assert_awaited_once_with(A, "")

    async def test_settings_without_name_leave_it_alone(self):
        resp = await self.put({"volume_lock": False})
        self.assertEqual(resp.status, 200)
        self.manager.set_friendly_name.assert_not_called()

    async def test_rejects_bad_names_before_changing_anything(self):
        for bad in (42, None, "x" * (FRIENDLY_NAME_MAX_LEN + 1), "line\nbreak", "tab\there"):
            if bad is None:
                continue  # None means "not sent"; covered by the next test
            with self.subTest(name=bad):
                resp = await self.put({"friendly_name": bad, "volume_lock": True})
                self.assertEqual(resp.status, 400)
        self.manager.set_friendly_name.assert_not_called()
        self.manager.update_device_settings.assert_not_called()

    async def test_null_and_nothing_else_is_rejected(self):
        resp = await self.put({"friendly_name": None})
        self.assertEqual(resp.status, 400)

    async def test_max_length_is_accepted(self):
        resp = await self.put({"friendly_name": "x" * FRIENDLY_NAME_MAX_LEN})
        self.assertEqual(resp.status, 200)


@unittest.skipIf(BluetoothAudioManager is None, _MANAGER_SKIP or "")
class ManagerFriendlyNameTest(unittest.TestCase):

    def build(self, names):
        m = BluetoothAudioManager.__new__(BluetoothAudioManager)
        m.store = MagicMock()
        m.store.get_friendly_name.side_effect = lambda addr: names.get(addr, "")
        m.store.get_device.side_effect = lambda addr: {"address": addr, "name": "ES-BT17"}
        return m

    def test_display_name_is_replaced_and_bluetooth_name_kept(self):
        m = self.build({A: "UKW 87.5"})
        devices = [{"address": A, "name": "ES-BT17"}, {"address": B, "name": "ES-BT17"}]
        m._apply_friendly_names(devices)
        self.assertEqual(devices[0], {"address": A, "name": "UKW 87.5",
                                      "bt_name": "ES-BT17", "friendly_name": "UKW 87.5"})
        self.assertEqual(devices[1], {"address": B, "name": "ES-BT17",
                                      "bt_name": "ES-BT17", "friendly_name": ""})

    def test_mpd_output_name_uses_friendly_name(self):
        m = self.build({A: "UKW 87.5"})
        self.assertEqual(m._mpd_output_name(A), "UKW 87.5")

    def test_mpd_output_name_falls_back_to_name_and_mac_suffix(self):
        m = self.build({})
        self.assertEqual(m._mpd_output_name(B), "ES-BT17 (51512)")


if __name__ == "__main__":
    unittest.main()
