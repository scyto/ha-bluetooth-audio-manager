"""Persistence store: device records, settings defaults, MPD port pool."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from bt_audio_manager.persistence.store import (
    DEFAULT_DEVICE_SETTINGS,
    MPD_PORT_MAX,
    MPD_PORT_MIN,
    PersistenceStore,
)

ADDR_A = "AA:BB:CC:DD:EE:01"
ADDR_B = "AA:BB:CC:DD:EE:02"


class StoreTestCase(unittest.IsolatedAsyncioTestCase):
    """Base: a store backed by a real file in a temp dir."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "paired_devices.json"
        self.store = PersistenceStore(str(self.path))
        await self.store.load()


class TestDeviceRecords(StoreTestCase):
    async def test_add_then_read_back_from_disk(self):
        await self.store.add_device(ADDR_A, "Kitchen Speaker")

        reloaded = PersistenceStore(str(self.path))
        await reloaded.load()
        self.assertEqual(len(reloaded.devices), 1)
        self.assertEqual(reloaded.get_device(ADDR_A)["name"], "Kitchen Speaker")

    async def test_adding_the_same_address_updates_rather_than_duplicates(self):
        await self.store.add_device(ADDR_A, "Old Name")
        await self.store.add_device(ADDR_A, "New Name", auto_connect=False)

        self.assertEqual(len(self.store.devices), 1)
        device = self.store.get_device(ADDR_A)
        self.assertEqual(device["name"], "New Name")
        self.assertFalse(device["auto_connect"])
        # paired_at is set once, on first pairing.
        self.assertIn("paired_at", device)

    async def test_devices_property_returns_a_copy(self):
        await self.store.add_device(ADDR_A, "Speaker")
        self.store.devices.clear()
        self.assertEqual(len(self.store.devices), 1)

    async def test_auto_connect_excludes_only_devices_opted_out(self):
        await self.store.add_device(ADDR_A, "A")
        await self.store.add_device(ADDR_B, "B", auto_connect=False)
        addrs = [d["address"] for d in self.store.auto_connect_devices]
        self.assertEqual(addrs, [ADDR_A])

    async def test_legacy_record_without_auto_connect_key_is_included(self):
        self.path.write_text(json.dumps({"devices": [{"address": ADDR_A, "name": "Legacy"}]}))
        store = PersistenceStore(str(self.path))
        await store.load()
        self.assertEqual([d["address"] for d in store.auto_connect_devices], [ADDR_A])

    async def test_remove_and_clear(self):
        await self.store.add_device(ADDR_A, "A")
        await self.store.add_device(ADDR_B, "B")
        await self.store.remove_device(ADDR_A)
        self.assertEqual([d["address"] for d in self.store.devices], [ADDR_B])
        await self.store.clear_all()
        self.assertEqual(self.store.devices, [])

    async def test_removing_an_unknown_address_is_a_no_op(self):
        await self.store.add_device(ADDR_A, "A")
        await self.store.remove_device("FF:FF:FF:FF:FF:FF")
        self.assertEqual(len(self.store.devices), 1)


class TestAddressNormalisation(StoreTestCase):
    """BlueZ reports upper case; REST callers and HA templates often don't.

    Without one canonical form a lower-case lookup misses the record and
    the settings endpoint creates a second one from defaults, which reads
    to the user as their MPD config resetting itself (issue #299).
    """

    async def test_address_is_stored_upper_case(self):
        await self.store.add_device(ADDR_A.lower(), "A")
        self.assertEqual(self.store.devices[0]["address"], ADDR_A)

    async def test_lookups_ignore_case(self):
        await self.store.add_device(ADDR_A, "A")
        self.assertIsNotNone(self.store.get_device(ADDR_A.lower()))
        self.assertEqual(self.store.get_device_settings(ADDR_A.lower())["audio_profile"], "a2dp")

    async def test_a_lower_case_write_updates_rather_than_duplicating(self):
        await self.store.add_device(ADDR_A, "A")
        await self.store.update_device_settings(ADDR_A.lower(), {"mpd_enabled": True})
        await self.store.set_mpd_port(ADDR_A.lower(), 6604)

        self.assertEqual(len(self.store.devices), 1)
        settings = self.store.get_device_settings(ADDR_A)
        self.assertIs(settings["mpd_enabled"], True)
        self.assertEqual(settings["mpd_port"], 6604)

    async def test_lower_case_add_does_not_duplicate_an_existing_record(self):
        await self.store.add_device(ADDR_A, "A")
        await self.store.update_device_settings(ADDR_A, {"mpd_enabled": True})
        await self.store.add_device(ADDR_A.lower(), "A")

        self.assertEqual(len(self.store.devices), 1)
        self.assertIs(self.store.get_device_settings(ADDR_A)["mpd_enabled"], True)

    async def test_remove_ignores_case(self):
        await self.store.add_device(ADDR_A, "A")
        await self.store.remove_device(ADDR_A.lower())
        self.assertEqual(self.store.devices, [])

    async def test_port_pool_sees_a_lower_case_record_as_taken(self):
        self.path.write_text(json.dumps({"devices": [
            {"address": ADDR_A.lower(), "name": "A", "mpd_port": MPD_PORT_MIN},
            {"address": ADDR_B, "name": "B"},
        ]}))
        store = PersistenceStore(str(self.path))
        await store.load()
        self.assertEqual(await store.allocate_mpd_port(ADDR_B), MPD_PORT_MIN + 1)

    async def test_load_heals_a_store_already_split_across_cases(self):
        # What a polluted store looks like: the real record, then the
        # default one a lower-case settings write appended after it.
        self.path.write_text(json.dumps({"devices": [
            {"address": ADDR_A, "name": "A", "mpd_enabled": True, "mpd_port": 6602},
            {"address": ADDR_A.lower(), "name": "A", "mpd_enabled": False, "mpd_port": None},
        ]}))
        store = PersistenceStore(str(self.path))
        await store.load()

        self.assertEqual(len(store.devices), 1)
        settings = store.get_device_settings(ADDR_A)
        self.assertIs(settings["mpd_enabled"], True)
        self.assertEqual(settings["mpd_port"], 6602)

    async def test_healed_store_is_persisted_on_the_next_write(self):
        self.path.write_text(json.dumps({"devices": [{"address": ADDR_A.lower(), "name": "A"}]}))
        store = PersistenceStore(str(self.path))
        await store.load()
        await store.update_device_settings(ADDR_A, {"idle_mode": "power_save"})

        written = json.loads(self.path.read_text())["devices"]
        self.assertEqual([d["address"] for d in written], [ADDR_A])


class TestPersistenceMechanics(StoreTestCase):
    async def test_save_is_atomic_and_leaves_no_temp_file(self):
        await self.store.add_device(ADDR_A, "A")
        leftovers = list(Path(self._tmp.name).glob("*.tmp"))
        self.assertEqual(leftovers, [], f"temp file not cleaned up: {leftovers}")

    async def test_corrupt_json_loads_as_empty_rather_than_raising(self):
        self.path.write_text("{ this is not json")
        store = PersistenceStore(str(self.path))
        await store.load()
        self.assertEqual(store.devices, [])

    async def test_missing_file_loads_as_empty(self):
        store = PersistenceStore(str(Path(self._tmp.name) / "absent.json"))
        await store.load()
        self.assertEqual(store.devices, [])

    async def test_legacy_data_path_is_migrated_on_load(self):
        # /data/paired_devices.json → /config/paired_devices.json. Patched
        # to temp paths; the real constants point at container locations.
        from bt_audio_manager.persistence import store as store_mod

        legacy = Path(self._tmp.name) / "legacy.json"
        legacy.write_text(json.dumps({"devices": [{"address": ADDR_A, "name": "Legacy"}]}))
        target = Path(self._tmp.name) / "migrated.json"

        original = store_mod.LEGACY_STORE_PATH
        store_mod.LEGACY_STORE_PATH = str(legacy)
        try:
            migrated = PersistenceStore(str(target))
            await migrated.load()
        finally:
            store_mod.LEGACY_STORE_PATH = original

        self.assertEqual(migrated.get_device(ADDR_A)["name"], "Legacy")
        self.assertTrue(target.exists(), "migrated file should be written to the new path")


class TestDeviceSettings(StoreTestCase):
    async def test_unknown_device_gets_plain_defaults(self):
        self.assertEqual(self.store.get_device_settings("FF:FF:FF:FF:FF:FF"), DEFAULT_DEVICE_SETTINGS)

    async def test_missing_keys_are_filled_with_defaults(self):
        await self.store.add_device(ADDR_A, "A")
        settings = self.store.get_device_settings(ADDR_A)
        self.assertEqual(set(settings), set(DEFAULT_DEVICE_SETTINGS))
        self.assertEqual(settings["audio_profile"], "a2dp")
        self.assertIs(settings["mpd_enabled"], False)

    async def test_update_persists_and_ignores_unknown_keys(self):
        await self.store.add_device(ADDR_A, "A")
        await self.store.update_device_settings(
            ADDR_A, {"idle_mode": "power_save", "not_a_real_setting": 1}
        )

        reloaded = PersistenceStore(str(self.path))
        await reloaded.load()
        self.assertEqual(reloaded.get_device_settings(ADDR_A)["idle_mode"], "power_save")
        self.assertNotIn("not_a_real_setting", reloaded.get_device(ADDR_A))

    async def test_update_on_unknown_device_returns_none(self):
        self.assertIsNone(await self.store.update_device_settings(ADDR_A, {"idle_mode": "power_save"}))

    async def test_legacy_keep_alive_flag_migrates_to_idle_mode(self):
        await self.store.add_device(ADDR_A, "A")
        self.store.get_device(ADDR_A)["keep_alive_enabled"] = True
        self.assertEqual(self.store.get_device_settings(ADDR_A)["idle_mode"], "keep_alive")

        self.store.get_device(ADDR_A)["keep_alive_enabled"] = False
        self.assertEqual(self.store.get_device_settings(ADDR_A)["idle_mode"], "default")

    async def test_explicit_idle_mode_wins_over_legacy_flag(self):
        await self.store.add_device(ADDR_A, "A")
        device = self.store.get_device(ADDR_A)
        device["keep_alive_enabled"] = True
        device["idle_mode"] = "auto_disconnect"
        self.assertEqual(self.store.get_device_settings(ADDR_A)["idle_mode"], "auto_disconnect")


class TestMpdPortAllocation(StoreTestCase):
    async def test_allocates_lowest_free_port(self):
        await self.store.add_device(ADDR_A, "A")
        await self.store.add_device(ADDR_B, "B")
        self.assertEqual(await self.store.allocate_mpd_port(ADDR_A), MPD_PORT_MIN)
        self.assertEqual(await self.store.allocate_mpd_port(ADDR_B), MPD_PORT_MIN + 1)

    async def test_allocation_is_idempotent(self):
        await self.store.add_device(ADDR_A, "A")
        first = await self.store.allocate_mpd_port(ADDR_A)
        self.assertEqual(await self.store.allocate_mpd_port(ADDR_A), first)

    async def test_released_port_is_reused_by_the_next_device(self):
        await self.store.add_device(ADDR_A, "A")
        await self.store.add_device(ADDR_B, "B")
        await self.store.allocate_mpd_port(ADDR_A)
        await self.store.allocate_mpd_port(ADDR_B)
        await self.store.release_mpd_port(ADDR_A)
        self.assertEqual(await self.store.allocate_mpd_port(ADDR_A), MPD_PORT_MIN)

    async def test_pool_exhaustion_returns_none(self):
        count = MPD_PORT_MAX - MPD_PORT_MIN + 1
        for i in range(count):
            addr = f"AA:BB:CC:DD:EE:{i:02X}"
            await self.store.add_device(addr, f"Speaker {i}")
            self.assertIsNotNone(await self.store.allocate_mpd_port(addr))

        overflow = "AA:BB:CC:DD:FF:FF"
        await self.store.add_device(overflow, "One too many")
        self.assertIsNone(await self.store.allocate_mpd_port(overflow))

    async def test_allocation_for_unknown_device_returns_none(self):
        self.assertIsNone(await self.store.allocate_mpd_port("FF:FF:FF:FF:FF:FF"))

    async def test_manual_port_rejects_out_of_range(self):
        await self.store.add_device(ADDR_A, "A")
        self.assertFalse(await self.store.set_mpd_port(ADDR_A, MPD_PORT_MIN - 1))
        self.assertFalse(await self.store.set_mpd_port(ADDR_A, MPD_PORT_MAX + 1))
        self.assertTrue(await self.store.set_mpd_port(ADDR_A, MPD_PORT_MAX))

    async def test_manual_port_rejects_collision_but_allows_self(self):
        await self.store.add_device(ADDR_A, "A")
        await self.store.add_device(ADDR_B, "B")
        self.assertTrue(await self.store.set_mpd_port(ADDR_A, 6605))
        self.assertFalse(await self.store.set_mpd_port(ADDR_B, 6605))
        # Re-asserting a device's own port is not a collision.
        self.assertTrue(await self.store.set_mpd_port(ADDR_A, 6605))

    async def test_manual_port_survives_a_reload(self):
        await self.store.add_device(ADDR_A, "A")
        await self.store.set_mpd_port(ADDR_A, 6607)

        reloaded = PersistenceStore(str(self.path))
        await reloaded.load()
        self.assertEqual(reloaded.get_device_settings(ADDR_A)["mpd_port"], 6607)

    async def test_allocation_skips_a_manually_assigned_port(self):
        await self.store.add_device(ADDR_A, "A")
        await self.store.add_device(ADDR_B, "B")
        await self.store.set_mpd_port(ADDR_A, MPD_PORT_MIN)
        self.assertEqual(await self.store.allocate_mpd_port(ADDR_B), MPD_PORT_MIN + 1)

    async def test_concurrent_allocations_do_not_collide(self):
        addrs = [f"AA:BB:CC:DD:EE:{i:02X}" for i in range(5)]
        for addr in addrs:
            await self.store.add_device(addr, addr)

        ports = await asyncio.gather(*(self.store.allocate_mpd_port(a) for a in addrs))
        self.assertEqual(len(set(ports)), len(addrs), f"duplicate ports handed out: {ports}")


if __name__ == "__main__":
    unittest.main()
