"""Discovery vs. active A2DP streams (#319).

A discovery scan competes with the A2DP links for airtime on the same
radio.  A user scan could freeze every stream with no recovery (HA kept
showing "playing"), and the silent RSSI bursts ran the same kind of
discovery every minute while audio was flowing.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from bt_audio_manager.audio.pulse import PulseAudioManager
    from bt_audio_manager.manager import BluetoothAudioManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

A = "41:42:E8:96:EE:34"
B = "41:42:86:95:15:12"
C = "41:42:03:37:0A:D0"


class FakeMpd:
    def __init__(self, state="play", running=True):
        self.state = state
        self.is_running = running
        self.restarts = 0

    async def get_status(self):
        return {"state": self.state}

    async def restart_playback(self):
        self.restarts += 1
        return True


def manager_with(mpds):
    manager = BluetoothAudioManager.__new__(BluetoothAudioManager)
    manager._mpd_instances = mpds
    manager.SCAN_RESUME_STAGGER = 0
    return manager


class ScanResumeTest(unittest.IsolatedAsyncioTestCase):

    async def test_only_playing_streams_are_collected(self):
        manager = manager_with({
            A: FakeMpd("play"), B: FakeMpd("stop"), C: FakeMpd("play", running=False),
        })
        self.assertEqual(await manager._playing_mpd_addresses(), [A])

    async def test_resume_restarts_each_stream_once(self):
        mpds = {A: FakeMpd(), B: FakeMpd()}
        manager = manager_with(mpds)
        await manager._resume_after_scan([A, B])
        self.assertEqual([mpds[A].restarts, mpds[B].restarts], [1, 1])

    async def test_resume_skips_daemons_that_went_away(self):
        mpds = {A: FakeMpd(running=False)}
        manager = manager_with(mpds)
        await manager._resume_after_scan([A, B])
        self.assertEqual(mpds[A].restarts, 0)

    async def _run_scan(self, *, cancel):
        manager = manager_with({})
        manager.adapter = MagicMock()
        manager.adapter.start_discovery = AsyncMock()
        manager.adapter.stop_discovery = AsyncMock()
        manager._cancel_scan_debounce = MagicMock()
        manager._broadcast_devices = AsyncMock()
        manager.event_bus = MagicMock()
        manager._scanning = True
        manager._resume_after_scan = AsyncMock()
        manager._fire_and_forget = lambda coro: asyncio.ensure_future(coro)
        task = asyncio.ensure_future(manager._run_scan(0 if not cancel else 60, resume=[A]))
        if cancel:
            await asyncio.sleep(0)
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        return manager

    async def test_finished_scan_resumes_streams(self):
        manager = await self._run_scan(cancel=False)
        manager._resume_after_scan.assert_awaited_once_with([A])

    async def test_cancelled_scan_leaves_resume_to_the_next_one(self):
        manager = await self._run_scan(cancel=True)
        manager._resume_after_scan.assert_not_called()


class RssiBurstTest(unittest.IsolatedAsyncioTestCase):

    async def _one_cycle(self, *, running_sinks):
        manager = BluetoothAudioManager.__new__(BluetoothAudioManager)
        manager.RSSI_REFRESH_INTERVAL = 0
        manager.RSSI_REFRESH_DURATION = 0
        manager._scanning = False
        manager._running_sinks = set(running_sinks)
        manager._poll_running_sinks = set()
        manager._device_connect_time = {A: 1.0}
        manager.adapter = MagicMock()
        manager.adapter.start_rssi_refresh = AsyncMock()
        manager.adapter.stop_rssi_refresh = AsyncMock()
        manager._rssi_cleanup = MagicMock()

        calls = 0
        real_sleep = asyncio.sleep

        async def one_round(delay):
            nonlocal calls
            calls += 1
            if calls > 2:  # first sleep = interval, second = burst duration
                raise asyncio.CancelledError
            await real_sleep(0)

        with patch("bt_audio_manager.manager.asyncio.sleep", one_round):
            await manager._rssi_refresh_loop()
        return manager

    async def test_no_burst_while_audio_is_running(self):
        manager = await self._one_cycle(running_sinks={"bluez_sink.41_42_E8_96_EE_34.a2dp_sink"})
        manager.adapter.start_rssi_refresh.assert_not_called()

    async def test_burst_runs_when_nothing_plays(self):
        manager = await self._one_cycle(running_sinks=set())
        manager.adapter.start_rssi_refresh.assert_awaited()


class SampleSpecCacheTest(unittest.IsolatedAsyncioTestCase):
    """Sink listing used to spawn a pactl client on every call (5 s poll + API).

    Spawning pactl is the prime suspect for the short dropout of #319, so
    it must never run on a timer while the sinks stay the same.
    """

    SPEC = {"format": "s16le", "rate": 44100, "channels": 2}
    A = "bluez_sink.A.a2dp_sink"
    B = "bluez_sink.B.a2dp_sink"

    def build(self, specs):
        manager = PulseAudioManager()
        manager._pactl_sample_specs = AsyncMock(return_value=specs)
        return manager

    async def test_unchanged_sinks_never_respawn_pactl(self):
        manager = self.build({self.A: self.SPEC})
        loop = asyncio.get_running_loop()
        for t in (1000.0, 1001.0, 1000.0 + 3600):  # an hour later: still cached
            with patch.object(loop, "time", return_value=t):
                await manager._cached_sample_specs({self.A: 1})
        self.assertEqual(manager._pactl_sample_specs.await_count, 1)

    async def test_new_sink_or_recreated_sink_rereads(self):
        manager = self.build({self.A: self.SPEC, self.B: self.SPEC})
        await manager._cached_sample_specs({self.A: 1})
        await manager._cached_sample_specs({self.A: 1, self.B: 2})  # new sink
        self.assertEqual(manager._pactl_sample_specs.await_count, 2)
        await manager._cached_sample_specs({self.A: 7, self.B: 2})  # A recreated
        self.assertEqual(manager._pactl_sample_specs.await_count, 3)
        await manager._cached_sample_specs({self.A: 7, self.B: 2})
        self.assertEqual(manager._pactl_sample_specs.await_count, 3)

    async def test_failing_pactl_is_not_retried_on_every_poll(self):
        manager = self.build({})
        loop = asyncio.get_running_loop()
        with patch.object(loop, "time", return_value=1000.0):
            await manager._cached_sample_specs({self.A: 1})
            await manager._cached_sample_specs({self.A: 1})
        self.assertEqual(manager._pactl_sample_specs.await_count, 1)
        with patch.object(loop, "time", return_value=1000.0 + manager.SPEC_RETRY_AFTER_FAILURE + 1):
            await manager._cached_sample_specs({self.A: 1})
        self.assertEqual(manager._pactl_sample_specs.await_count, 2)


if __name__ == "__main__":
    unittest.main()
