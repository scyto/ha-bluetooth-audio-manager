"""Prefer 44.1 kHz: re-open an A2DP stream a speaker set up itself at 48 kHz."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from bt_audio_manager.config import AppConfig
    from bt_audio_manager.manager import BluetoothAudioManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

A = "41:42:00:00:09:70"
CFG_48K = b"\x11\x15\x02\x35"   # 48 kHz, joint stereo, bitpool 2..53
CFG_44K = b"\x21\x15\x02\x35"   # 44.1 kHz


class ParseTest(unittest.TestCase):

    def test_sbc_rates(self):
        f = BluetoothAudioManager.sbc_sample_rate
        self.assertEqual(f(CFG_48K), 48000)
        self.assertEqual(f(CFG_44K), 44100)
        self.assertEqual(f([0x21, 0x15]), 44100)  # dbus-next may hand a list
        self.assertIsNone(f(b""))
        self.assertIsNone(f(None))


def build(formats, *, playing=True, prefer=True):
    m = BluetoothAudioManager.__new__(BluetoothAudioManager)
    m.config = AppConfig()
    m.config.a2dp_prefer_44k = prefer
    m._rate_fix_attempts = {}
    m._rate_fix_running = set()
    m._device_lifecycle_locks = {}
    m._a2dp_transport_format = AsyncMock(side_effect=list(formats))
    device = MagicMock(disconnect_profile=AsyncMock(), connect_profile=AsyncMock())
    m.managed_devices = {A: device}
    mpd = MagicMock(is_running=True, handle_command=AsyncMock(), resume=AsyncMock(return_value=True),
                    get_status=AsyncMock(return_value={"state": "play" if playing else "stop"}),
                    playback_snapshot=AsyncMock(return_value={"uri": "http://radio/bob", "playing": playing}))
    m._mpd_instances = {A: mpd}
    m.pulse = MagicMock(wait_for_bt_sink=AsyncMock(return_value="bluez_sink.x"))
    m._poll_card_profile = AsyncMock(return_value=True)
    m.force_reconnect_device = AsyncMock(return_value=True)
    return m, device, mpd


class EnsureRateTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._p = patch("bt_audio_manager.manager.asyncio.sleep", AsyncMock())
        self._p.start()

    async def asyncTearDown(self):
        self._p.stop()

    async def test_44k_is_left_alone(self):
        m, device, mpd = build([(0, 44100)])
        self.assertTrue((await m.ensure_a2dp_rate(A)).startswith("ok"))
        device.disconnect_profile.assert_not_called()

    async def test_48k_is_reconnected_and_playback_resumed(self):
        m, device, mpd = build([(0, 48000), (0, 44100)])
        new_mpd = MagicMock(is_running=True, handle_command=AsyncMock(), resume=AsyncMock(return_value=True))

        async def reconnect(address):
            m._mpd_instances[address] = new_mpd  # the reconnect starts a new daemon
            return True

        m.force_reconnect_device = AsyncMock(side_effect=reconnect)
        result = await m.ensure_a2dp_rate(A)
        self.assertEqual(result, "reconnected (44100)")
        m.force_reconnect_device.assert_awaited_once_with(A)
        # Only re-opening the A2DP stream left an ES-BT17 playing at 48 kHz
        device.disconnect_profile.assert_not_called()
        device.connect_profile.assert_not_called()
        self.assertEqual([c.args[0] for c in mpd.handle_command.await_args_list], ["Stop"])
        # The new daemon starts with an empty queue: the station is queued again
        new_mpd.resume.assert_awaited_once_with("http://radio/bob")

    async def test_station_is_noted_when_the_speaker_is_not_back_yet(self):
        import tempfile, os
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        m, device, mpd = build([(0, 48000), None])
        m.PLAYBACK_RESUME_FILE = os.path.join(tmp.name, "resume.json")

        async def reconnect(address):
            m._mpd_instances.pop(address, None)  # disconnected, not back yet
            raise RuntimeError("br-connection-page-timeout")

        m.force_reconnect_device = AsyncMock(side_effect=reconnect)
        await m.ensure_a2dp_rate(A)
        # The daemon started by the automatic reconnect plays it again
        new = MagicMock(is_running=True, resume=AsyncMock(return_value=True),
                        get_status=AsyncMock(return_value={"state": "stop"}))
        self.assertTrue(await m._resume_playback(A, new))
        new.resume.assert_awaited_once_with("http://radio/bob")

    async def test_failed_reconnect_is_left_to_auto_reconnect(self):
        m, device, mpd = build([(0, 48000), None])
        m.force_reconnect_device = AsyncMock(side_effect=RuntimeError("br-connection-page-timeout"))
        m._mpd_instances = {A: mpd}
        result = await m.ensure_a2dp_rate(A)
        self.assertEqual(result, "reconnected (None)")
        self.assertEqual(m._rate_fix_running, set())

    async def test_idle_mpd_is_not_started(self):
        m, device, mpd = build([(0, 48000), (0, 44100)], playing=False)
        await m.ensure_a2dp_rate(A)
        mpd.handle_command.assert_not_called()

    async def test_non_sbc_or_disabled_untouched(self):
        m, device, _ = build([(2, None)])  # AAC
        await m.ensure_a2dp_rate(A)
        device.disconnect_profile.assert_not_called()
        m, device, _ = build([(0, 48000)], prefer=False)
        self.assertEqual(await m.ensure_a2dp_rate(A), "disabled")
        device.disconnect_profile.assert_not_called()

    async def test_gives_up_after_two_attempts(self):
        m, device, _ = build([(0, 48000)] * 10)
        for _ in range(2):
            await m.ensure_a2dp_rate(A)
        self.assertEqual(await m.ensure_a2dp_rate(A), "gave up")
        self.assertEqual(m.force_reconnect_device.await_count, 2)


if __name__ == "__main__":
    unittest.main()
