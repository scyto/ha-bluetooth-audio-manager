"""Only subscribe to the BlueZ signals this app needs.

Upstream added one rule for every BlueZ signal.  With Home Assistant
scanning passively on a second adapter, that delivered a constant stream of
RSSI/ManufacturerData changes for unrelated BLE devices to this app, each
unmarshalled in Python and dropped.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

try:
    from bt_audio_manager.manager import BluetoothAudioManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc


def build(log_level="info"):
    manager = BluetoothAudioManager.__new__(BluetoothAudioManager)
    manager.config = MagicMock(log_level=log_level)
    manager.bus = MagicMock()
    manager.bus.call = AsyncMock()
    manager._bluez_match_rules = set()
    return manager


class MatchRuleTest(unittest.IsolatedAsyncioTestCase):

    def test_default_only_object_manager_signals_up_front(self):
        self.assertEqual(
            build()._initial_bluez_match_rules(),
            ["type='signal',sender='org.bluez',path='/'"],
        )

    def test_debug_keeps_the_broad_rule_for_diagnosis(self):
        self.assertEqual(
            build("debug")._initial_bluez_match_rules(),
            ["type='signal',sender='org.bluez'"],
        )

    def test_adapter_rule_covers_its_whole_subtree(self):
        self.assertEqual(
            BluetoothAudioManager._adapter_match_rule("/org/bluez/hci0"),
            "type='signal',sender='org.bluez',path_namespace='/org/bluez/hci0'",
        )

    async def test_each_rule_is_added_once(self):
        manager = build()
        rule = BluetoothAudioManager._adapter_match_rule("/org/bluez/hci0")
        await manager._add_bluez_match(rule)
        await manager._add_bluez_match(rule)
        self.assertEqual(manager.bus.call.await_count, 1)
        message = manager.bus.call.await_args.args[0]
        self.assertEqual(message.member, "AddMatch")
        self.assertEqual(message.body, [rule])


if __name__ == "__main__":
    unittest.main()
