"""Prefer 44.1 kHz: re-open an A2DP stream a speaker set up itself at 48 kHz."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from bt_audio_manager.config import AppConfig
    from bt_audio_manager.manager import BluetoothAudioManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

A = "41:42:03:37:0A:D0"
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
    mpd = MagicMock(is_running=True, handle_command=AsyncMock(),
                    get_status=AsyncMock(return_value={"state": "play" if playing else "stop"}))
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

    async def test_48k_is_reopened_and_playback_resumed(self):
        m, device, mpd = build([(0, 48000), (0, 44100)])
        result = await m.ensure_a2dp_rate(A)
        self.assertEqual(result, "re-opened (44100)")
        device.disconnect_profile.assert_awaited_once()
        device.connect_profile.assert_awaited_once()
        self.assertEqual([c.args[0] for c in mpd.handle_command.await_args_list], ["Stop", "Play"])

    async def test_card_profile_is_switched_to_a2dp(self):
        m, device, _ = build([(0, 48000), (0, 44100)])
        await m.ensure_a2dp_rate(A)
        m._poll_card_profile.assert_awaited_once()
        self.assertEqual(m._poll_card_profile.await_args.args, (A, "a2dp"))
        m.force_reconnect_device.assert_not_called()

    async def test_missing_sink_falls_back_to_reconnect(self):
        m, device, _ = build([(0, 48000), (0, 44100)])
        m.pulse.wait_for_bt_sink = AsyncMock(return_value=None)
        await m.ensure_a2dp_rate(A)
        m.force_reconnect_device.assert_awaited_once_with(A)

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
        self.assertEqual(device.disconnect_profile.await_count, 2)


if __name__ == "__main__":
    unittest.main()
