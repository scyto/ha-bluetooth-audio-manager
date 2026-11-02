"""Per-device volume lock: hold MPD, stream and speaker volume at 100 %.

Three independent layers can make a speaker quieter: MPD's software volume
(what HA's media_player shows), the MPD stream's volume in PulseAudio, and
the Bluetooth sink volume (BlueZ maps it to the speaker's AVRCP absolute
volume, so the speaker buttons move it).  With the lock on, each is reset.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from bt_audio_manager.audio.mpd import MPDManager
    from bt_audio_manager.audio.pulse import PulseAudioManager
    from bt_audio_manager.manager import BluetoothAudioManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

A = "41:42:E8:96:EE:34"
B = "41:42:86:95:15:12"
SINK_A = "bluez_sink.41_42_E8_96_EE_34.a2dp_sink"


class FakeStore:
    def __init__(self, locked):
        self.locked = set(locked)

    def get_device_settings(self, address):
        return {"volume_lock": address in self.locked}


def build_manager(locked=(A,)):
    m = BluetoothAudioManager.__new__(BluetoothAudioManager)
    m.store = FakeStore(locked)
    m._mpd_instances = {}
    m._last_pa_volume = {}
    m._volume_lock_resets = {}
    m.recent_avrcp = []
    m.event_bus = MagicMock()
    m.pulse = MagicMock()
    m.pulse.set_sink_volume = AsyncMock(return_value=True)
    m.pulse.get_sink_for_address = AsyncMock(return_value=SINK_A)
    m.pulse.enforce_volume_lock_all = AsyncMock(return_value=0)
    m._fire_and_forget = lambda coro: asyncio.ensure_future(coro)
    return m


class SpeakerVolumeTest(unittest.IsolatedAsyncioTestCase):

    async def test_locked_speaker_volume_goes_back_and_mpd_is_not_dragged(self):
        m = build_manager()
        mpd = MagicMock(is_running=True, set_volume=AsyncMock())
        m._mpd_instances[A] = mpd
        m._on_pa_volume_change(SINK_A, 40, False)
        await asyncio.sleep(0)
        m.pulse.set_sink_volume.assert_awaited_once_with(SINK_A, 100)
        mpd.set_volume.assert_not_called()

    async def test_unlocked_speaker_volume_still_syncs_to_mpd(self):
        m = build_manager(locked=())
        mpd = MagicMock(is_running=True, set_volume=AsyncMock())
        m._mpd_instances[A] = mpd
        m._on_pa_volume_change(SINK_A, 40, False)
        await asyncio.sleep(0)
        mpd.set_volume.assert_awaited_once_with(40)
        m.pulse.set_sink_volume.assert_not_called()

    async def test_100_percent_needs_no_reset(self):
        m = build_manager()
        m._on_pa_volume_change(SINK_A, 100, False)
        await asyncio.sleep(0)
        m.pulse.set_sink_volume.assert_not_called()

    async def test_resets_are_rate_limited(self):
        m = build_manager()
        with patch("bt_audio_manager.manager.time.monotonic", return_value=1000.0):
            for _ in range(m.VOLUME_LOCK_MAX_RESETS + 3):
                await m._enforce_sink_volume_lock(A, SINK_A, 30)
        self.assertEqual(m.pulse.set_sink_volume.await_count, m.VOLUME_LOCK_MAX_RESETS)

    async def test_apply_brings_all_layers_to_100(self):
        m = build_manager()
        mpd = MagicMock(is_running=True, set_volume=AsyncMock(), volume_lock=False)
        m._mpd_instances[A] = mpd
        await m._apply_volume_lock(A)
        self.assertTrue(mpd.volume_lock)
        mpd.set_volume.assert_awaited_once_with(100)
        m.pulse.set_sink_volume.assert_awaited_once_with(SINK_A, 100)
        m.pulse.enforce_volume_lock_all.assert_awaited_once()


class MpdVolumeTest(unittest.IsolatedAsyncioTestCase):

    def build(self, locked):
        mpd = MPDManager(address=A, port=6600, speaker_name="Speaker")
        mpd.volume_lock = locked
        mpd.set_volume = AsyncMock()
        return mpd

    async def test_locked_mpd_volume_goes_back_to_100(self):
        mpd = self.build(True)
        self.assertTrue(await mpd._enforce_volume_lock({"volume": "35", "state": "play"}))
        mpd.set_volume.assert_awaited_once_with(100)

    async def test_unlocked_mpd_volume_is_left_alone(self):
        mpd = self.build(False)
        self.assertFalse(await mpd._enforce_volume_lock({"volume": "35"}))
        mpd.set_volume.assert_not_called()

    async def test_no_mixer_or_already_100_is_left_alone(self):
        mpd = self.build(True)
        for status in ({"volume": "100"}, {"volume": "-1"}, {}, {"state": "unknown"}):
            with self.subTest(status=status):
                self.assertFalse(await mpd._enforce_volume_lock(status))
        mpd.set_volume.assert_not_called()


def stream(index, volume, mac=A):
    return SimpleNamespace(
        index=index, sink=1,
        volume=SimpleNamespace(value_flat=volume),
        proplist={"module-stream-restore.id": f"bt-audio-manager:{mac}"},
    )


class StreamVolumeTest(unittest.IsolatedAsyncioTestCase):

    def build(self, streams, locked=(A,)):
        pa = PulseAudioManager()
        pa._pulse = MagicMock()
        pa._pulse.sink_input_info = AsyncMock(side_effect=lambda i: next(s for s in streams if s.index == i))
        pa._pulse.sink_input_list = AsyncMock(return_value=list(streams))
        pa._pulse.volume_set_all_chans = AsyncMock()
        pa.set_volume_lock_resolver(lambda addr: addr in locked)
        return pa

    async def test_locked_stream_goes_back_to_100(self):
        pa = self.build([stream(10, 0.6)])
        self.assertTrue(await pa._enforce_locked_stream_volume(10))
        pa._pulse.volume_set_all_chans.assert_awaited_once()
        self.assertEqual(pa._pulse.volume_set_all_chans.await_args.args[1], 1.0)

    async def test_unlocked_or_full_streams_are_left_alone(self):
        pa = self.build([stream(10, 0.6, mac=B), stream(11, 1.0)])
        self.assertEqual(await pa.enforce_volume_lock_all(), 0)
        pa._pulse.volume_set_all_chans.assert_not_called()

    async def test_foreign_streams_are_never_touched(self):
        foreign = SimpleNamespace(index=20, sink=1, volume=SimpleNamespace(value_flat=0.3),
                                  proplist={"application.name": "Music Assistant"})
        pa = self.build([foreign])
        self.assertFalse(await pa._enforce_locked_stream_volume(20))
        pa._pulse.volume_set_all_chans.assert_not_called()


class SettingsApiTest(unittest.TestCase):

    def test_default_is_off(self):
        from bt_audio_manager.persistence.store import DEFAULT_DEVICE_SETTINGS
        self.assertIs(DEFAULT_DEVICE_SETTINGS["volume_lock"], False)


from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from bt_audio_manager.web.api import create_api_routes


class VolumeLockApiTest(AioHTTPTestCase):
    """The switch is reachable via PUT /api/devices/{address}/settings."""

    async def get_application(self):
        self.manager = MagicMock()
        self.manager.store.get_device.return_value = {"address": A}
        self.manager.store.get_device_settings.return_value = {"volume_lock": True}
        self.manager.update_device_settings = AsyncMock(return_value={"address": A})
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def test_switch_on(self):
        resp = await self.client.put(f"/api/devices/{A}/settings", json={"volume_lock": True})
        self.assertEqual(resp.status, 200)
        self.manager.update_device_settings.assert_awaited_once_with(A, {"volume_lock": True})

    async def test_non_boolean_is_rejected(self):
        resp = await self.client.put(f"/api/devices/{A}/settings", json={"volume_lock": "yes"})
        self.assertEqual(resp.status, 400)
        self.manager.update_device_settings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
