"""Several Bluetooth adapters in one app instance (per-speaker "adapter").

Three A2DP streams on one Realtek stick overload it now and then; every
overload makes PulseAudio lower the SBC bitrate for a long time (14.11.2026).
With a second stick one speaker moves there: its "adapter" setting names the
stick, it is paired there, and everything else (connect, reconnect, reset,
watch for the adapter going away) follows.  These tests pin the decisions;
BlueZ is mocked.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase
from dbus_next import MessageType, Variant

from bt_audio_manager.manager import BluetoothAudioManager
from bt_audio_manager.persistence.store import DEFAULT_DEVICE_SETTINGS
from bt_audio_manager.web.api import create_api_routes

MAIN = "8A:88:4B:00:00:A1"
STICK2 = "8A:88:4B:00:00:02"
HA = "AC:12:2F:00:00:01"
BOX_A = "41:42:00:00:00:0A"
BOX_B = "41:42:00:00:00:0B"
MAIN_PATH = "/org/bluez/hci1"
STICK2_PATH = "/org/bluez/hci2"


def make_store(assign: dict[str, str]):
    store = MagicMock()
    store.devices = [{"address": a} for a in (BOX_A, BOX_B)]

    def settings(address):
        s = dict(DEFAULT_DEVICE_SETTINGS)
        s["adapter"] = assign.get(address, "")
        return s

    store.get_device_settings.side_effect = settings
    store.update_device_settings = AsyncMock(return_value={"address": BOX_B})
    store.add_device = AsyncMock()
    return store


def make_manager(assign=None, *, bound=True):
    m = BluetoothAudioManager.__new__(BluetoothAudioManager)
    m._adapter_path = MAIN_PATH
    m._adapter_address = MAIN
    m._adapter_lost = False
    m._adapter_restart_task = None
    m.adapter = MagicMock(name="main-adapter")
    m.store = make_store(assign if assign is not None else {BOX_B: STICK2})
    m._extra_adapters = {}
    m._extra_adapter_paths = {}
    m._missing_extra_adapters = set()
    m._lost_extra_adapters = set()
    m.managed_devices = {}
    m.media_player = None
    m.bus = MagicMock()
    m._broadcast_status = MagicMock()
    m._add_bluez_match = AsyncMock()
    if bound:
        second = MagicMock(name="stick2")
        m._extra_adapters[STICK2] = second
        m._extra_adapter_paths[STICK2] = STICK2_PATH
    return m


def adapters_listing(*entries):
    return [{"path": p, "address": a, "name": p.rsplit("/", 1)[-1]} for p, a in entries]


class AssignmentTest(unittest.TestCase):

    def test_store_default_is_main_adapter(self):
        self.assertEqual(DEFAULT_DEVICE_SETTINGS["adapter"], "")

    def test_assigned_adapter(self):
        m = make_manager({BOX_B: STICK2.lower(), BOX_A: MAIN})
        self.assertEqual(m._assigned_adapter_mac(BOX_B), STICK2)
        self.assertEqual(m._assigned_adapter_mac(BOX_A), "")  # the main adapter's MAC = main

    def test_wanted_adapters(self):
        m = make_manager()
        self.assertEqual(m._wanted_extra_adapter_macs(), {STICK2: [BOX_B]})

    def test_preferred_path(self):
        m = make_manager()
        self.assertEqual(m._preferred_adapter_path(BOX_B), STICK2_PATH)
        self.assertEqual(m._preferred_adapter_path(BOX_A), MAIN_PATH)

    def test_lost_or_unbound_adapter_falls_back_to_main(self):
        m = make_manager()
        m._lost_extra_adapters.add(STICK2)
        self.assertEqual(m._preferred_adapter_path(BOX_B), MAIN_PATH)
        m = make_manager(bound=False)
        self.assertEqual(m._preferred_adapter_path(BOX_B), MAIN_PATH)

    def test_active_adapters(self):
        m = make_manager()
        self.assertEqual(m._active_adapter_paths(), [MAIN_PATH, STICK2_PATH])
        self.assertEqual([mac for mac, _, _ in m._active_adapters()], [MAIN, STICK2])
        self.assertIs(m._adapter_wrapper(""), m.adapter)
        self.assertIs(m._adapter_wrapper(STICK2.lower()), m._extra_adapters[STICK2])
        self.assertIsNone(m._adapter_wrapper(HA))


def objects_with(*paths):
    return {p: {"org.bluez.Device1": {}} for p in paths}


class FindAdapterTest(unittest.IsolatedAsyncioTestCase):

    async def _find(self, m, objects, address):
        obj_mgr = MagicMock()
        obj_mgr.call_get_managed_objects = AsyncMock(return_value=objects)
        m.bus.introspect = AsyncMock()
        m.bus.get_proxy_object.return_value.get_interface.return_value = obj_mgr
        return await m._find_device_adapter(address)

    async def test_paired_on_both_uses_own_adapter(self):
        m = make_manager()
        objs = objects_with(f"{MAIN_PATH}/dev_41_42_00_00_00_0B", f"{STICK2_PATH}/dev_41_42_00_00_00_0B")
        self.assertEqual(await self._find(m, objs, BOX_B), STICK2_PATH)

    async def test_not_yet_paired_on_new_adapter_keeps_working_on_main(self):
        m = make_manager()
        objs = objects_with(f"{MAIN_PATH}/dev_41_42_00_00_00_0B")
        self.assertEqual(await self._find(m, objs, BOX_B), MAIN_PATH)

    async def test_main_speakers_stay_on_main(self):
        m = make_manager()
        objs = objects_with(f"{MAIN_PATH}/dev_41_42_00_00_00_0A", f"{STICK2_PATH}/dev_41_42_00_00_00_0A")
        self.assertEqual(await self._find(m, objs, BOX_A), MAIN_PATH)


class BindTest(unittest.IsolatedAsyncioTestCase):

    async def test_present_adapter_is_bound(self):
        m = make_manager(bound=False)
        m.media_player = MagicMock()
        m.media_player.add_adapter = AsyncMock()
        wrapper = MagicMock()
        wrapper.initialize = AsyncMock()
        with patch("bt_audio_manager.manager.BluezAdapter") as BA:
            BA.list_all = AsyncMock(return_value=adapters_listing(
                (MAIN_PATH, MAIN), (STICK2_PATH, STICK2), ("/org/bluez/hci0", HA)))
            BA.return_value = wrapper
            await m._bind_extra_adapters()
        self.assertIs(m._extra_adapters[STICK2], wrapper)
        self.assertEqual(m._extra_adapter_paths[STICK2], STICK2_PATH)
        m._add_bluez_match.assert_awaited_once()
        self.assertIn(STICK2_PATH, m._add_bluez_match.await_args.args[0])
        m.media_player.add_adapter.assert_awaited_once_with(STICK2_PATH)

    async def test_missing_adapter_is_remembered_not_fatal(self):
        m = make_manager(bound=False)
        with patch("bt_audio_manager.manager.BluezAdapter") as BA:
            BA.list_all = AsyncMock(return_value=adapters_listing((MAIN_PATH, MAIN)))
            await m._bind_extra_adapters()
        self.assertEqual(m._missing_extra_adapters, {STICK2})
        self.assertEqual(m._extra_adapters, {})

    async def test_unpowered_adapter_is_skipped(self):
        m = make_manager(bound=False)
        wrapper = MagicMock()
        wrapper.initialize = AsyncMock(side_effect=RuntimeError("not powered"))
        with patch("bt_audio_manager.manager.BluezAdapter") as BA:
            BA.list_all = AsyncMock(return_value=adapters_listing((MAIN_PATH, MAIN), (STICK2_PATH, STICK2)))
            BA.return_value = wrapper
            await m._bind_extra_adapters()
        self.assertEqual(m._extra_adapters, {})
        self.assertIn(STICK2, m._missing_extra_adapters)


def removed(path):
    return SimpleNamespace(
        message_type=MessageType.SIGNAL, member="InterfacesRemoved", path="/",
        body=[path, ["org.bluez.Adapter1"]],
    )


def added(path, address):
    return SimpleNamespace(
        message_type=MessageType.SIGNAL, member="InterfacesAdded", path="/",
        body=[path, {"org.bluez.Adapter1": {"Address": Variant("s", address)}}],
    )


class LifecycleTest(unittest.IsolatedAsyncioTestCase):

    def build(self, **kw):
        m = make_manager(**kw)
        m._restart_after_adapter_return = AsyncMock()
        m._bind_extra_adapters = AsyncMock()
        return m

    async def test_missing_adapter_appearing_is_bound(self):
        m = self.build(bound=False)
        m._missing_extra_adapters.add(STICK2)
        m._on_adapter_lifecycle_signal(added(STICK2_PATH, STICK2))
        await asyncio.sleep(0)
        m._bind_extra_adapters.assert_awaited_once()
        m._restart_after_adapter_return.assert_not_called()

    async def test_bound_adapter_returning_restarts(self):
        m = self.build()
        m._on_adapter_lifecycle_signal(removed(STICK2_PATH))
        self.assertIn(STICK2, m._lost_extra_adapters)
        m._on_adapter_lifecycle_signal(added("/org/bluez/hci3", STICK2))
        await m._adapter_restart_task
        m._restart_after_adapter_return.assert_awaited_once()

    async def test_bound_adapter_announced_again_unchanged_is_ignored(self):
        m = self.build()
        m._on_adapter_lifecycle_signal(added(STICK2_PATH, STICK2))
        self.assertIsNone(m._adapter_restart_task)

    async def test_foreign_adapter_is_ignored(self):
        m = self.build()
        m._on_adapter_lifecycle_signal(added("/org/bluez/hci0", HA))
        await asyncio.sleep(0)
        self.assertIsNone(m._adapter_restart_task)
        m._bind_extra_adapters.assert_not_awaited()

    async def test_main_adapter_behaviour_unchanged(self):
        m = self.build()
        m._on_adapter_lifecycle_signal(removed(MAIN_PATH))
        self.assertTrue(m._adapter_lost)
        m._on_adapter_lifecycle_signal(added("/org/bluez/hci4", MAIN))
        await m._adapter_restart_task
        m._restart_after_adapter_return.assert_awaited_once()


class PairOnSecondAdapterTest(unittest.IsolatedAsyncioTestCase):

    def build(self):
        m = make_manager(bound=False)
        m._a2dp_attempts = {}
        m._connecting = set()
        m.event_bus = MagicMock()
        m._broadcast_all = AsyncMock()
        m._ensure_device_object = AsyncMock()
        m._remove_from_other_adapters = AsyncMock(return_value=[MAIN_PATH])
        m._block_on_other_adapters = AsyncMock(return_value=[MAIN_PATH])
        m.connect_device = AsyncMock(return_value=True)
        m._adapter_path_for_mac = AsyncMock(return_value=STICK2_PATH)

        async def bind(mac, path, speakers=None):
            m._extra_adapters[mac] = MagicMock()
            m._extra_adapter_paths[mac] = path
            return m._extra_adapters[mac]
        m._bind_adapter = AsyncMock(side_effect=bind)
        device = MagicMock()
        device.adapter_path = STICK2_PATH
        device.pair = AsyncMock()
        device.set_trusted = AsyncMock()
        device.get_name = AsyncMock(return_value="ES-BT17")
        device.get_uuids = AsyncMock(return_value=["0000110b-0000-1000-8000-00805f9b34fb"])
        m._get_or_create_device = AsyncMock(return_value=device)
        return m, device

    async def test_pairs_on_the_given_adapter_and_moves_the_speaker(self):
        m, device = self.build()
        result = await m.pair_device(BOX_B, adapter=STICK2)
        self.assertTrue(result["connected"])
        m._ensure_device_object.assert_awaited_once_with(BOX_B, STICK2_PATH)
        m._get_or_create_device.assert_awaited_once_with(BOX_B, adapter_path=STICK2_PATH)
        device.pair.assert_awaited_once()
        m.store.update_device_settings.assert_awaited_once_with(BOX_B, {"adapter": STICK2})
        m._remove_from_other_adapters.assert_awaited_once_with(BOX_B, keep_path=STICK2_PATH)
        m._block_on_other_adapters.assert_awaited_once_with(BOX_B, STICK2_PATH, True)
        m.connect_device.assert_awaited_once()

    async def test_failed_move_unblocks_the_old_adapter(self):
        m, _ = self.build()
        m._ensure_device_object = AsyncMock(side_effect=RuntimeError("not found"))
        with self.assertRaises(RuntimeError):
            await m.pair_device(BOX_B, adapter=STICK2)
        self.assertEqual(
            [c.args for c in m._block_on_other_adapters.await_args_list],
            [(BOX_B, STICK2_PATH, True), (BOX_B, STICK2_PATH, False)],
        )
        m._remove_from_other_adapters.assert_not_awaited()
        self.assertNotIn(BOX_B, m._connecting)

    async def test_uses_the_assigned_adapter_without_parameter(self):
        m, _ = self.build()
        await m.pair_device(BOX_B)  # BOX_B is assigned to STICK2
        m._ensure_device_object.assert_awaited_once_with(BOX_B, STICK2_PATH)

    async def test_main_adapter_pairing_unchanged(self):
        m, _ = self.build()
        await m.pair_device(BOX_A)
        m._ensure_device_object.assert_not_awaited()
        m._block_on_other_adapters.assert_not_awaited()
        m._get_or_create_device.assert_awaited_once_with(BOX_A, adapter_path=None)
        m.store.update_device_settings.assert_not_awaited()
        m._remove_from_other_adapters.assert_not_awaited()

    async def test_absent_adapter_is_an_error(self):
        m, _ = self.build()
        m._adapter_path_for_mac = AsyncMock(return_value=None)
        with self.assertRaises(RuntimeError):
            await m.pair_device(BOX_B, adapter=STICK2)
        self.assertNotIn(BOX_B, m._connecting)


class RemoveOldPairingTest(unittest.IsolatedAsyncioTestCase):

    async def test_only_the_apps_other_adapters(self):
        m = make_manager()
        m._device_object_paths = AsyncMock(return_value=[
            f"{MAIN_PATH}/dev_41_42_00_00_00_0B",
            f"{STICK2_PATH}/dev_41_42_00_00_00_0B",
            "/org/bluez/hci0/dev_41_42_00_00_00_0B",  # Home Assistant's adapter
        ])
        iface = MagicMock()
        iface.call_remove_device = AsyncMock()
        m.bus.introspect = AsyncMock()
        m.bus.get_proxy_object.return_value.get_interface.return_value = iface
        removed_from = await m._remove_from_other_adapters(BOX_B, keep_path=STICK2_PATH)
        self.assertEqual(removed_from, [MAIN_PATH])
        iface.call_remove_device.assert_awaited_once_with(f"{MAIN_PATH}/dev_41_42_00_00_00_0B")


class DedupeTest(unittest.TestCase):

    def test_prefers_connected_then_own_adapter(self):
        m = make_manager()
        on_main = {"address": BOX_B, "path": f"{MAIN_PATH}/dev_x", "paired": True, "connected": False}
        on_stick = {"address": BOX_B, "path": f"{STICK2_PATH}/dev_x", "paired": True, "connected": False}
        other = {"address": BOX_A, "path": f"{MAIN_PATH}/dev_y", "paired": True, "connected": True}
        self.assertEqual(m._dedupe_devices([on_main, other, on_stick]), [on_stick, other])
        on_main_connected = dict(on_main, connected=True)
        self.assertEqual(m._dedupe_devices([on_stick, on_main_connected]), [on_main_connected])


class ResetSelectionTest(unittest.IsolatedAsyncioTestCase):

    def build(self):
        m = make_manager()
        m._adapter_reset_lock = asyncio.Lock()
        m._adapter_reset_at = None
        m._adapter_resetting = False
        m.reconnect_service = MagicMock()
        m.reconnect_service.reconnect_all = AsyncMock()
        m.ADAPTER_RESET_OFF_SECONDS = 0
        for a in (m.adapter, m._extra_adapters[STICK2]):
            a.set_powered = AsyncMock()
            a.is_powered = AsyncMock(return_value=True)
        return m

    async def test_default_resets_every_adapter(self):
        m = self.build()
        result = await m.reset_adapter("soft")
        self.assertEqual(result["adapters"], [MAIN_PATH, STICK2_PATH])
        m.adapter.set_powered.assert_any_await(False)
        m._extra_adapters[STICK2].set_powered.assert_any_await(False)

    async def test_one_adapter(self):
        m = self.build()
        result = await m.reset_adapter("soft", adapter=STICK2.lower())
        self.assertEqual(result["adapters"], [STICK2_PATH])
        m.adapter.set_powered.assert_not_awaited()

    async def test_unknown_adapter(self):
        m = self.build()
        with self.assertRaises(ValueError):
            await m.reset_adapter("soft", adapter=HA)
        self.assertIsNone(m._adapter_reset_at)


class AdapterSettingApiTest(AioHTTPTestCase):

    async def get_application(self):
        self.manager = MagicMock()
        self.manager.store.get_device.return_value = {"address": BOX_B}
        self.manager.store.get_device_settings.return_value = {"adapter": ""}
        self.manager.store.get_friendly_name.return_value = ""
        self.manager.update_device_settings = AsyncMock(return_value={"address": BOX_B})
        self.manager._get_ha_bluetooth_macs = AsyncMock(return_value={HA})
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def put(self, value):
        return await self.client.put(f"/api/devices/{BOX_B}/settings", json={"adapter": value})

    async def test_mac_is_stored_upper_case(self):
        resp = await self.put(STICK2.lower())
        self.assertEqual(resp.status, 200)
        self.manager.update_device_settings.assert_awaited_once_with(BOX_B, {"adapter": STICK2})

    async def test_empty_means_main_adapter(self):
        resp = await self.put("")
        self.assertEqual(resp.status, 200)
        self.manager.update_device_settings.assert_awaited_once_with(BOX_B, {"adapter": ""})

    async def test_rejects_garbage(self):
        resp = await self.put("hci2")
        self.assertEqual(resp.status, 400)
        self.manager.update_device_settings.assert_not_awaited()

    async def test_refuses_home_assistants_adapter(self):
        resp = await self.put(HA)
        self.assertEqual(resp.status, 409)
        self.manager.update_device_settings.assert_not_awaited()


class PairApiTest(AioHTTPTestCase):

    async def get_application(self):
        self.manager = MagicMock()
        self.manager.pair_device = AsyncMock(return_value={"address": BOX_B, "connected": True})
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def test_adapter_is_passed(self):
        resp = await self.client.post("/api/pair", json={"address": BOX_B, "adapter": STICK2.lower()})
        self.assertEqual(resp.status, 200)
        self.manager.pair_device.assert_awaited_once_with(BOX_B, adapter=STICK2)

    async def test_without_adapter(self):
        resp = await self.client.post("/api/pair", json={"address": BOX_B})
        self.assertEqual(resp.status, 200)
        self.manager.pair_device.assert_awaited_once_with(BOX_B, adapter=None)

    async def test_bad_adapter(self):
        resp = await self.client.post("/api/pair", json={"address": BOX_B, "adapter": "x"})
        self.assertEqual(resp.status, 400)


if __name__ == "__main__":
    unittest.main()
