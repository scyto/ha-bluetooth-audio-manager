"""MPD daemon liveness and the restart decision built on it.

MPD can exit on its own — the common trigger is its PulseAudio sink going
away while the speaker is dropping in and out.  If the add-on keeps
treating that dead daemon as running, the HA media_player entity stays
unavailable forever and nothing says why (issue #299).
"""

import unittest

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


if __name__ == "__main__":
    unittest.main()
