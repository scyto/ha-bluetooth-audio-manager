"""Volume lock level: hold locked speakers at a chosen level, not only 100 %.

FM transmitters such as the ES-BT17 can distort at full input level, so the
lock can hold MPD's volume lower.  The level sits in MPD (it scales the audio
digitally before Bluetooth, whatever the speaker does with AVRCP volume); the
MPD stream and the speaker's own volume stay at 100 %.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from bt_audio_manager import config as config_mod
from bt_audio_manager.config import AppConfig
from bt_audio_manager.web.api import create_api_routes

try:
    from bt_audio_manager.audio.mpd import MPDManager
    from bt_audio_manager.manager import BluetoothAudioManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

A = "41:42:E8:96:EE:34"
B = "41:42:86:95:15:12"
SINK_A = "bluez_sink.41_42_E8_96_EE_34.a2dp_sink"


class MpdLevelTest(unittest.IsolatedAsyncioTestCase):

    def build(self, level, locked=True):
        mpd = MPDManager(address=A, port=6600, speaker_name="Speaker")
        mpd.volume_lock = locked
        mpd.volume_lock_level = level
        mpd.set_volume = AsyncMock()
        return mpd

    async def test_default_level_is_100(self):
        self.assertEqual(MPDManager(address=A, port=6600, speaker_name="S").volume_lock_level, 100)

    async def test_volume_goes_to_the_level(self):
        mpd = self.build(85)
        self.assertTrue(await mpd._enforce_volume_lock({"volume": "100"}))
        mpd.set_volume.assert_awaited_once_with(85)

    async def test_volume_at_the_level_is_left_alone(self):
        mpd = self.build(85)
        self.assertFalse(await mpd._enforce_volume_lock({"volume": "85"}))
        mpd.set_volume.assert_not_called()

    async def test_unlocked_ignores_the_level(self):
        mpd = self.build(85, locked=False)
        self.assertFalse(await mpd._enforce_volume_lock({"volume": "40"}))
        mpd.set_volume.assert_not_called()


def build_manager(level=85):
    m = BluetoothAudioManager.__new__(BluetoothAudioManager)
    m.config = AppConfig()
    m.config.volume_lock_level = level
    m._mpd_instances = {}
    m._last_pa_volume = {}
    m.pulse = MagicMock()
    m.pulse.set_sink_volume = AsyncMock(return_value=True)
    m.pulse.get_sink_for_address = AsyncMock(return_value=SINK_A)
    m.pulse.enforce_volume_lock_all = AsyncMock(return_value=0)
    return m


class ManagerLevelTest(unittest.IsolatedAsyncioTestCase):

    async def test_apply_holds_mpd_at_level_and_speaker_at_100(self):
        m = build_manager(85)
        mpd = MagicMock(is_running=True, set_volume=AsyncMock(), volume_lock=False)
        m._mpd_instances[A] = mpd
        await m._apply_volume_lock(A)
        self.assertTrue(mpd.volume_lock)
        self.assertEqual(mpd.volume_lock_level, 85)
        mpd.set_volume.assert_awaited_once_with(85)
        m.pulse.set_sink_volume.assert_awaited_once_with(SINK_A, 100)

    async def test_changing_the_level_moves_locked_speakers_now(self):
        m = build_manager(100)
        locked = MagicMock(is_running=True, set_volume=AsyncMock(), volume_lock=True)
        unlocked = MagicMock(is_running=True, set_volume=AsyncMock(), volume_lock=False)
        m._mpd_instances = {A: locked, B: unlocked}
        await m.set_volume_lock_level(80)
        self.assertEqual(m.config.volume_lock_level, 80)
        locked.set_volume.assert_awaited_once_with(80)
        unlocked.set_volume.assert_not_called()
        self.assertEqual(unlocked.volume_lock_level, 80)  # ready if locked later

    def test_level_falls_back_to_100_without_config(self):
        m = BluetoothAudioManager.__new__(BluetoothAudioManager)
        self.assertEqual(m._volume_lock_level(), 100)


class ConfigLevelTest(unittest.TestCase):

    def load_with(self, settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "settings.json"
        path.write_text(json.dumps(settings))
        with patch.object(config_mod, "SETTINGS_PATH", str(path)), \
             patch.object(config_mod, "OPTIONS_PATH", str(Path(tmp.name) / "none.json")):
            return AppConfig.load()

    def test_saved_level_is_loaded(self):
        self.assertEqual(self.load_with({"volume_lock_level": 85}).volume_lock_level, 85)

    def test_missing_or_bad_level_means_100(self):
        for raw in ({}, {"volume_lock_level": 5}, {"volume_lock_level": 101},
                    {"volume_lock_level": "85"}, {"volume_lock_level": True}):
            with self.subTest(raw=raw):
                self.assertEqual(self.load_with(raw).volume_lock_level, 100)

    def test_level_is_a_runtime_setting(self):
        self.assertEqual(AppConfig().runtime_settings["volume_lock_level"], 100)


class LevelApiTest(AioHTTPTestCase):

    async def get_application(self):
        self.manager = MagicMock()
        self.manager.config.auto_reconnect = True
        self.manager.config.runtime_settings = {"auto_reconnect": True, "volume_lock_level": 100}
        self.manager.volume_lock_summary.return_value = {
            "volume_lock_all": True, "volume_lock_any": True,
        }
        self.calls = []
        self.manager.set_volume_lock_level = AsyncMock(
            side_effect=lambda v: self.calls.append(("level", v)))
        self.manager.set_volume_lock_all = AsyncMock(
            side_effect=lambda v: self.calls.append(("all", v)))
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def test_level_is_applied_and_saved(self):
        resp = await self.client.put("/api/settings", json={"volume_lock_level": 85})
        self.assertEqual(resp.status, 200)
        self.manager.set_volume_lock_level.assert_awaited_once_with(85)
        self.manager.config.save_settings.assert_called()

    async def test_level_comes_before_switching_the_lock_on(self):
        resp = await self.client.put(
            "/api/settings", json={"volume_lock_all": True, "volume_lock_level": 90})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.calls, [("level", 90), ("all", True)])

    async def test_bad_levels_are_rejected(self):
        for bad in (9, 101, "85", 85.5, True, None):
            with self.subTest(level=bad):
                resp = await self.client.put("/api/settings", json={"volume_lock_level": bad})
                self.assertEqual(resp.status, 400)
        self.manager.set_volume_lock_level.assert_not_called()

    async def test_other_settings_leave_the_level_alone(self):
        resp = await self.client.put("/api/settings", json={"scan_duration_seconds": 30})
        self.assertEqual(resp.status, 200)
        self.manager.set_volume_lock_level.assert_not_called()


if __name__ == "__main__":
    unittest.main()
