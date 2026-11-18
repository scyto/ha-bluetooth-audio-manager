"""Names of your own for Bluetooth adapters (like speaker friendly names).

With two sticks in one instance, "hci1"/"hci2" says little; a name such as
"Stick 87.5" shows in the adapter list, the speaker settings and on the
device cards.  Kept by MAC and not cleared with the devices.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from bt_audio_manager.manager import BluetoothAudioManager
from bt_audio_manager.persistence.store import PersistenceStore
from bt_audio_manager.web.api import create_api_routes

STICK1 = "8A:88:4B:00:00:A1"
STICK2 = "8A:88:4B:00:00:02"


class StoreTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paired_devices.json"

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_set_persist_clear(self):
        store = PersistenceStore(str(self.path))
        await store.load()
        self.assertEqual(await store.set_adapter_name(STICK2.lower(), "  Stick 87.5 "), "Stick 87.5")
        again = PersistenceStore(str(self.path))
        await again.load()
        self.assertEqual(again.get_adapter_name(STICK2), "Stick 87.5")
        self.assertEqual(await again.set_adapter_name(STICK2, ""), "")
        self.assertEqual(again.get_adapter_name(STICK2), "")
        self.assertNotIn("adapter_names", json.loads(self.path.read_text()))

    async def test_clear_all_devices_keeps_adapter_names(self):
        store = PersistenceStore(str(self.path))
        await store.load()
        await store.add_device("41:42:00:00:00:0A", "Box")
        await store.set_adapter_name(STICK1, "Stick 1")
        await store.clear_all()
        self.assertEqual(store.get_adapter_name(STICK1), "Stick 1")


class LabelTest(unittest.TestCase):

    def test_label_uses_name_else_hci(self):
        m = BluetoothAudioManager.__new__(BluetoothAudioManager)
        m._adapter_path = "/org/bluez/hci1"
        m._adapter_address = STICK1
        m._extra_adapter_paths = {STICK2: "/org/bluez/hci2"}
        m.store = MagicMock()
        m.store.get_adapter_name.side_effect = lambda mac: {STICK2: "Stick 87.5"}.get(mac, "")
        self.assertEqual(m.adapter_label("hci2"), "Stick 87.5")
        self.assertEqual(m.adapter_label("/org/bluez/hci2"), "Stick 87.5")
        self.assertEqual(m.adapter_label("hci1"), "hci1")
        self.assertEqual(m.adapter_label("hci0"), "hci0")  # not one of ours


class RenameApiTest(AioHTTPTestCase):

    async def get_application(self):
        self.manager = MagicMock()
        self.manager.set_adapter_name = AsyncMock(side_effect=lambda mac, name: name)
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def test_sets_name(self):
        resp = await self.client.put(f"/api/adapters/{STICK2.lower()}/name", json={"name": " Stick 87.5 "})
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"address": STICK2, "friendly_name": "Stick 87.5"})
        self.manager.set_adapter_name.assert_awaited_once_with(STICK2, "Stick 87.5")

    async def test_bad_address(self):
        resp = await self.client.put("/api/adapters/hci1/name", json={"name": "x"})
        self.assertEqual(resp.status, 400)

    async def test_bad_names(self):
        for body in ({"name": 5}, {"name": "a\nb"}, {"name": "x" * 65}, {}):
            with self.subTest(body=body):
                resp = await self.client.put(f"/api/adapters/{STICK2}/name", json=body)
                self.assertEqual(resp.status, 400)
        self.manager.set_adapter_name.assert_not_awaited()


class InfoLabelsTest(AioHTTPTestCase):
    """The footer lists every adapter the app drives, main first."""

    async def get_application(self):
        self.manager = MagicMock()
        self.manager._adapter_path = "/org/bluez/hci2"
        self.manager._extra_adapter_paths = {STICK2: "/org/bluez/hci1"}
        self.manager.adapter_label.side_effect = lambda p: {"hci2": "Logilink alt", "hci1": "Logilink neu"}[p.rsplit("/", 1)[-1]]
        self.manager.config.bt_adapter_is_mac = True
        self.manager.config.bt_adapter = STICK1
        self.manager._get_mpd_password.return_value = None
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def test_info_lists_all_adapters(self):
        resp = await self.client.get("/api/info")
        data = await resp.json()
        self.assertEqual(data["adapter_friendly_name"], "Logilink alt")
        self.assertEqual(data["adapter_labels"], ["Logilink alt", "Logilink neu"])


if __name__ == "__main__":
    unittest.main()
