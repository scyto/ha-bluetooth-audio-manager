"""Detecting "audio is flowing" — and keeping discovery off while it is.

pulsectl's sink state is an EnumValue without ``.name``; the event monitor
compared ``"<EnumValue sink/source-state=running>"`` with ``"running"``, so
the running/idle callbacks never fired.  The RSSI burst guard built on them
never engaged, and the 5 s discovery burst kept running every minute on the
radio carrying the A2DP links — a short audible dropout each time.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import pulsectl
    from bt_audio_manager.audio.pulse import sink_state_name
    from bt_audio_manager.manager import BluetoothAudioManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc


class SinkStateNameTest(unittest.TestCase):

    def test_real_pulsectl_enum_values(self):
        for name in ("running", "idle", "suspended"):
            with self.subTest(name=name):
                self.assertEqual(sink_state_name(getattr(pulsectl.PulseStateEnum, name)), name)

    def test_repr_string_fallback(self):
        self.assertEqual(sink_state_name("<EnumValue sink/source-state=idle>"), "idle")

    def test_object_with_name(self):
        self.assertEqual(sink_state_name(SimpleNamespace(name="running")), "running")


class RssiBurstGuardTest(unittest.IsolatedAsyncioTestCase):

    async def one_cycle(self, *, event_running=(), poll_running=()):
        manager = BluetoothAudioManager.__new__(BluetoothAudioManager)
        manager.RSSI_REFRESH_INTERVAL = 0
        manager.RSSI_REFRESH_DURATION = 0
        manager._scanning = False
        manager._running_sinks = set(event_running)
        manager._poll_running_sinks = set(poll_running)
        manager._device_connect_time = {"41:42:E8:96:EE:34": 1.0}
        manager.adapter = MagicMock()
        manager.adapter.start_rssi_refresh = AsyncMock()
        manager.adapter.stop_rssi_refresh = AsyncMock()
        manager._rssi_cleanup = MagicMock()
        calls = 0
        real_sleep = asyncio.sleep

        async def one_round(delay):
            nonlocal calls
            calls += 1
            if calls > 2:
                raise asyncio.CancelledError
            await real_sleep(0)

        with patch("bt_audio_manager.manager.asyncio.sleep", one_round):
            await manager._rssi_refresh_loop()
        return manager.adapter.start_rssi_refresh

    async def test_poll_alone_keeps_discovery_off(self):
        # The PA event path missed it — the poll still blocks the burst.
        start = await self.one_cycle(poll_running={"bluez_sink.41_42_E8_96_EE_34.a2dp_sink"})
        start.assert_not_called()

    async def test_events_alone_keep_discovery_off(self):
        start = await self.one_cycle(event_running={"bluez_sink.41_42_E8_96_EE_34.a2dp_sink"})
        start.assert_not_called()

    async def test_burst_runs_only_when_nothing_plays(self):
        start = await self.one_cycle()
        start.assert_awaited()


class HaManagedAdaptersTest(unittest.TestCase):
    """An adapter set to *Ignore* in HA must not be flagged as HA-managed."""

    def test_ignored_entry_does_not_count(self):
        entries = [
            {"domain": "bluetooth", "source": "integration_discovery",
             "title": "Intel Corporate 0032 (4C:77:CB:78:46:A9)", "unique_id": None},
            {"domain": "bluetooth", "source": "ignore",
             "title": "hci1 (8A:88:4B:83:57:6D) Realtek Bluetooth 5.3 Radio (0bda:a729)",
             "unique_id": "8A:88:4B:83:57:6D"},
            {"domain": "mpd", "source": "user", "title": "Music Player Daemon"},
        ]
        self.assertEqual(
            BluetoothAudioManager._ha_bluetooth_macs_from_entries(entries),
            {"4C:77:CB:78:46:A9"},
        )

    def test_disabled_entry_still_counts(self):
        entries = [{"domain": "bluetooth", "source": "user", "disabled_by": "user",
                    "title": "x (00:1A:7D:DA:71:11)"}]
        self.assertEqual(
            BluetoothAudioManager._ha_bluetooth_macs_from_entries(entries),
            {"00:1A:7D:DA:71:11"},
        )


if __name__ == "__main__":
    unittest.main()
