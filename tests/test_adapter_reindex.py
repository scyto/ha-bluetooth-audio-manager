"""Adapter re-index: notice when *our* adapter comes back and restart.

The adapter path is resolved once at startup.  When the USB adapter drops
off the bus and re-enumerates (often as hci2 instead of hci0), every proxy
built on the old path is stale and reconnects fail with "Method Connect
doesn't exist" until the app is restarted by hand.

These tests pin the decision: which ObjectManager signals trigger a restart.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from dbus_next import MessageType, Variant
    from bt_audio_manager.manager import BluetoothAudioManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

OURS = "00:1A:7D:DA:71:13"
OTHER = "8C:F8:C5:11:22:33"


def removed(path):
    return SimpleNamespace(
        message_type=MessageType.SIGNAL, member="InterfacesRemoved", path="/",
        body=[path, ["org.freedesktop.DBus.Properties", "org.bluez.Adapter1"]],
    )


def added(path, address):
    return SimpleNamespace(
        message_type=MessageType.SIGNAL, member="InterfacesAdded", path="/",
        body=[path, {"org.bluez.Adapter1": {"Address": Variant("s", address)}}],
    )


class AdapterLifecycleTest(unittest.IsolatedAsyncioTestCase):

    def build(self, *, path="/org/bluez/hci0", address=OURS):
        manager = BluetoothAudioManager.__new__(BluetoothAudioManager)
        manager._adapter_path = path
        manager._adapter_address = address
        manager._adapter_lost = False
        manager._adapter_restart_task = None
        manager._broadcast_status = MagicMock()
        manager._restart_after_adapter_return = AsyncMock()
        return manager

    async def settle(self, manager):
        if manager._adapter_restart_task:
            await manager._adapter_restart_task

    async def test_reindexed_adapter_triggers_restart(self):
        manager = self.build()
        manager._on_adapter_lifecycle_signal(removed("/org/bluez/hci0"))
        self.assertTrue(manager._adapter_lost)
        manager._restart_after_adapter_return.assert_not_called()

        manager._on_adapter_lifecycle_signal(added("/org/bluez/hci2", OURS))
        await self.settle(manager)
        manager._restart_after_adapter_return.assert_awaited_once()

    async def test_same_path_return_after_loss_triggers_restart(self):
        manager = self.build()
        manager._on_adapter_lifecycle_signal(removed("/org/bluez/hci0"))
        manager._on_adapter_lifecycle_signal(added("/org/bluez/hci0", OURS))
        await self.settle(manager)
        manager._restart_after_adapter_return.assert_awaited_once()

    async def test_mac_match_is_case_insensitive(self):
        manager = self.build()
        manager._on_adapter_lifecycle_signal(removed("/org/bluez/hci0"))
        manager._on_adapter_lifecycle_signal(added("/org/bluez/hci2", OURS.lower()))
        await self.settle(manager)
        manager._restart_after_adapter_return.assert_awaited_once()

    async def test_other_adapter_appearing_is_ignored(self):
        # e.g. the adapter Home Assistant's own Bluetooth integration uses
        manager = self.build()
        manager._on_adapter_lifecycle_signal(removed("/org/bluez/hci0"))
        manager._on_adapter_lifecycle_signal(added("/org/bluez/hci1", OTHER))
        await asyncio.sleep(0)
        self.assertIsNone(manager._adapter_restart_task)

    async def test_other_adapter_disappearing_is_ignored(self):
        manager = self.build()
        manager._on_adapter_lifecycle_signal(removed("/org/bluez/hci1"))
        self.assertFalse(manager._adapter_lost)

    async def test_device_objects_coming_and_going_are_ignored(self):
        manager = self.build()
        device_path = "/org/bluez/hci0/dev_41_42_E8_96_EE_34"
        manager._on_adapter_lifecycle_signal(SimpleNamespace(
            message_type=MessageType.SIGNAL, member="InterfacesRemoved", path="/",
            body=[device_path, ["org.bluez.Device1"]],
        ))
        manager._on_adapter_lifecycle_signal(SimpleNamespace(
            message_type=MessageType.SIGNAL, member="InterfacesAdded", path="/",
            body=[device_path, {"org.bluez.Device1": {}}],
        ))
        await asyncio.sleep(0)
        self.assertFalse(manager._adapter_lost)
        self.assertIsNone(manager._adapter_restart_task)

    async def test_our_adapter_announced_without_a_loss_at_same_path_is_ignored(self):
        manager = self.build()
        manager._on_adapter_lifecycle_signal(added("/org/bluez/hci0", OURS))
        await asyncio.sleep(0)
        self.assertIsNone(manager._adapter_restart_task)

    async def test_unknown_own_mac_disables_the_feature(self):
        manager = self.build(address=None)
        manager._on_adapter_lifecycle_signal(removed("/org/bluez/hci0"))
        manager._on_adapter_lifecycle_signal(added("/org/bluez/hci2", OURS))
        await asyncio.sleep(0)
        self.assertIsNone(manager._adapter_restart_task)

    async def test_only_one_restart_is_scheduled(self):
        manager = self.build()
        gate = asyncio.Event()

        async def slow_restart():
            await gate.wait()

        manager._restart_after_adapter_return = MagicMock(side_effect=slow_restart)
        manager._on_adapter_lifecycle_signal(removed("/org/bluez/hci0"))
        manager._on_adapter_lifecycle_signal(added("/org/bluez/hci2", OURS))
        manager._on_adapter_lifecycle_signal(added("/org/bluez/hci2", OURS))
        self.assertEqual(manager._restart_after_adapter_return.call_count, 1)
        gate.set()
        await self.settle(manager)

    async def test_non_signal_messages_are_ignored(self):
        manager = self.build()
        manager._on_adapter_lifecycle_signal(SimpleNamespace(
            message_type=MessageType.METHOD_CALL, member="InterfacesRemoved", path="/",
            body=["/org/bluez/hci0", ["org.bluez.Adapter1"]],
        ))
        self.assertFalse(manager._adapter_lost)


class RestartAfterReturnTest(unittest.IsolatedAsyncioTestCase):

    def build(self, *, supervisor_ok):
        manager = BluetoothAudioManager.__new__(BluetoothAudioManager)
        manager.ADAPTER_RETURN_SETTLE = 0
        manager._broadcast_status = MagicMock()
        manager._request_self_restart = AsyncMock(return_value=supervisor_ok)
        manager.restart_requested = asyncio.Event()
        return manager

    async def test_uses_supervisor_restart_when_available(self):
        manager = self.build(supervisor_ok=True)
        await manager._restart_after_adapter_return()
        manager._request_self_restart.assert_awaited_once()
        self.assertFalse(manager.restart_requested.is_set())

    async def test_falls_back_to_process_restart(self):
        manager = self.build(supervisor_ok=False)
        await manager._restart_after_adapter_return()
        self.assertTrue(manager.restart_requested.is_set())

    async def test_no_supervisor_token_means_no_supervisor_restart(self):
        manager = BluetoothAudioManager.__new__(BluetoothAudioManager)
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(await manager._request_self_restart())


if __name__ == "__main__":
    unittest.main()
