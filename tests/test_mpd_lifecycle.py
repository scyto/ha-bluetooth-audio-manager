"""MPD daemon liveness and the restart decision built on it.

MPD can exit on its own — the common trigger is its PulseAudio sink going
away while the speaker is dropping in and out.  If the add-on keeps
treating that dead daemon as running, the HA media_player entity stays
unavailable forever and nothing says why (issue #299).
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

try:
    from bt_audio_manager.audio.mpd import MPDManager
    from bt_audio_manager.manager import BluetoothAudioManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

ADDR = "AA:BB:CC:DD:EE:FF"


class FakeProcess:
    """Just the bit of asyncio.subprocess.Process that liveness reads."""

    def __init__(self, returncode=None):
        self.returncode = returncode


class TestIsRunning(unittest.TestCase):
    def build(self):
        return MPDManager(address=ADDR, port=6600, speaker_name="Speaker")

    def test_a_started_daemon_is_running(self):
        mpd = self.build()
        mpd._running = True
        mpd._process = FakeProcess()
        self.assertTrue(mpd.is_running)

    def test_a_daemon_that_exited_is_not_running(self):
        mpd = self.build()
        mpd._running = True
        mpd._process = FakeProcess(returncode=1)
        self.assertFalse(mpd.is_running)

    def test_a_daemon_that_exited_cleanly_is_not_running(self):
        # returncode 0 is still gone — only None means "still alive".
        mpd = self.build()
        mpd._running = True
        mpd._process = FakeProcess(returncode=0)
        self.assertFalse(mpd.is_running)

    def test_never_started_is_not_running(self):
        self.assertFalse(self.build().is_running)


class FakeStore:
    def __init__(self, mpd_enabled=True):
        self._settings = {"mpd_enabled": mpd_enabled}

    def get_device_settings(self, address):
        return dict(self._settings)


class FakeMpd:
    def __init__(self, running):
        self.is_running = running
        self.stopped = False

    async def stop(self):
        self.stopped = True


class TestRestartDecision(unittest.IsolatedAsyncioTestCase):
    """_start_mpd_if_enabled's handling of an instance it already holds.

    ``pulse`` is None so the method returns right after that decision —
    starting a real daemon needs a PulseAudio server.
    """

    def build(self, instance, *, mpd_enabled=True):
        manager = BluetoothAudioManager.__new__(BluetoothAudioManager)
        manager.store = FakeStore(mpd_enabled)
        manager.pulse = None
        manager._mpd_instances = {ADDR: instance}
        manager._mpd_start_locks = {}
        return manager

    async def test_a_live_instance_is_left_alone(self):
        live = FakeMpd(running=True)
        manager = self.build(live)
        await manager._start_mpd_if_enabled(ADDR)
        self.assertFalse(live.stopped)
        self.assertIs(manager._mpd_instances.get(ADDR), live)

    async def test_a_dead_instance_is_reaped_so_a_restart_can_follow(self):
        dead = FakeMpd(running=False)
        manager = self.build(dead)
        await manager._start_mpd_if_enabled(ADDR)
        self.assertTrue(dead.stopped, "dead daemon's client/reader task not cleaned up")
        self.assertNotIn(ADDR, manager._mpd_instances)

    async def test_disabled_device_is_untouched(self):
        dead = FakeMpd(running=False)
        manager = self.build(dead, mpd_enabled=False)
        await manager._start_mpd_if_enabled(ADDR)
        self.assertIs(manager._mpd_instances.get(ADDR), dead)


SINK = "bluez_sink.AA_BB_CC_DD_EE_FF.a2dp_sink"


class FakeDevice:
    def __init__(self, connected=True):
        self.connected = connected

    async def is_connected(self):
        return self.connected


class TestSelfHeal(unittest.IsolatedAsyncioTestCase):
    """Sink-poll self-heal: a connected device with a sink but no live MPD.

    The add-on only starts MPD at fixed moments (connect, reconnect, ...).
    If the sink was not there yet, or the daemon died later, nothing tried
    again and the HA media_player stayed unavailable until mpd_enabled was
    toggled by hand.
    """

    def build(self, *, instance=None, mpd_enabled=True, connected=True):
        manager = BluetoothAudioManager.__new__(BluetoothAudioManager)
        manager.store = FakeStore(mpd_enabled)
        manager._mpd_instances = {ADDR: instance} if instance else {}
        manager._connecting = set()
        manager.managed_devices = {ADDR: FakeDevice(connected)}
        manager._device_lifecycle_locks = {}
        manager._mpd_self_heal_at = {}
        manager._mpd_missing_since = {}
        manager._start_mpd_if_enabled = AsyncMock()
        return manager

    async def poll(self, manager, at, sinks=None):
        sinks = [{"name": SINK}] if sinks is None else sinks
        with patch("bt_audio_manager.manager.time.monotonic", return_value=at):
            await manager._self_heal_mpd(sinks)

    async def test_missing_mpd_is_started_after_the_grace_period(self):
        manager = self.build()
        await self.poll(manager, 100.0)
        manager._start_mpd_if_enabled.assert_not_called()  # normal connect path's turn
        await self.poll(manager, 105.0)
        manager._start_mpd_if_enabled.assert_not_called()
        await self.poll(manager, 110.0)
        manager._start_mpd_if_enabled.assert_awaited_once_with(ADDR)

    async def test_dead_daemon_is_replaced(self):
        manager = self.build(instance=FakeMpd(running=False))
        await self.poll(manager, 100.0)
        await self.poll(manager, 111.0)
        manager._start_mpd_if_enabled.assert_awaited_once_with(ADDR)

    async def test_live_daemon_is_left_alone(self):
        manager = self.build(instance=FakeMpd(running=True))
        await self.poll(manager, 100.0)
        await self.poll(manager, 200.0)
        manager._start_mpd_if_enabled.assert_not_called()

    async def test_mpd_disabled_is_left_alone(self):
        manager = self.build(mpd_enabled=False)
        await self.poll(manager, 100.0)
        await self.poll(manager, 200.0)
        manager._start_mpd_if_enabled.assert_not_called()

    async def test_disconnected_device_is_left_alone(self):
        # A sink can linger for a moment after the link drops.
        manager = self.build(connected=False)
        await self.poll(manager, 100.0)
        await self.poll(manager, 200.0)
        manager._start_mpd_if_enabled.assert_not_called()

    async def test_connect_in_progress_is_left_alone(self):
        manager = self.build()
        manager._connecting.add(ADDR)
        await self.poll(manager, 100.0)
        await self.poll(manager, 200.0)
        manager._start_mpd_if_enabled.assert_not_called()

    async def test_unknown_device_is_left_alone(self):
        manager = self.build()
        manager.managed_devices = {}
        await self.poll(manager, 100.0)
        await self.poll(manager, 200.0)
        manager._start_mpd_if_enabled.assert_not_called()

    async def test_retries_are_rate_limited(self):
        manager = self.build()  # start stays a no-op, so MPD keeps missing
        await self.poll(manager, 100.0)
        await self.poll(manager, 110.0)   # 1st attempt
        await self.poll(manager, 115.0)   # within cooldown
        await self.poll(manager, 130.0)   # still within cooldown
        self.assertEqual(manager._start_mpd_if_enabled.await_count, 1)
        await self.poll(manager, 140.0)   # cooldown over
        self.assertEqual(manager._start_mpd_if_enabled.await_count, 2)

    async def test_grace_restarts_when_the_sink_goes_away(self):
        manager = self.build()
        await self.poll(manager, 100.0)
        await self.poll(manager, 105.0, sinks=[])  # sink gone
        await self.poll(manager, 110.0)            # back: grace starts again
        manager._start_mpd_if_enabled.assert_not_called()
        await self.poll(manager, 120.0)
        manager._start_mpd_if_enabled.assert_awaited_once_with(ADDR)

    async def test_lower_case_sink_names_match_stored_devices(self):
        manager = self.build()
        sinks = [{"name": SINK.replace("AA_BB_CC_DD_EE_FF", "aa_bb_cc_dd_ee_ff")}]
        await self.poll(manager, 100.0, sinks)
        await self.poll(manager, 110.0, sinks)
        manager._start_mpd_if_enabled.assert_awaited_once_with(ADDR)


class TestStartSerialisation(unittest.IsolatedAsyncioTestCase):
    """Concurrent start requests for one device must not spawn two daemons."""

    async def test_concurrent_starts_run_one_at_a_time(self):
        manager = BluetoothAudioManager.__new__(BluetoothAudioManager)
        manager._mpd_start_locks = {}
        active = 0
        peak = 0

        async def fake_locked(address):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

        manager._start_mpd_if_enabled_locked = fake_locked
        await asyncio.gather(*(manager._start_mpd_if_enabled(ADDR) for _ in range(3)))
        self.assertEqual(peak, 1)


if __name__ == "__main__":
    unittest.main()
