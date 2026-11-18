"""A station that was playing plays on after an app restart or a speaker drop.

MPD cannot keep a state file in the add-on container, so a new daemon starts
with an empty queue; the app notes the station when it stops a daemon and
queues it again when that speaker's daemon is back.
"""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from bt_audio_manager.manager import BluetoothAudioManager

A = "41:42:00:00:09:70"
URI = "http://streams.example/bob"


def build(tmpdir):
    m = BluetoothAudioManager.__new__(BluetoothAudioManager)
    m.PLAYBACK_RESUME_FILE = os.path.join(tmpdir, "playback_resume.json")
    m._mpd_instances = {}
    return m


def mpd(playing=True, uri=URI, running=True, state="stop"):
    d = MagicMock(is_running=running, stop=AsyncMock(), resume=AsyncMock(return_value=True))
    d.playback_snapshot = AsyncMock(return_value={"uri": uri, "playing": playing} if uri else None)
    d.get_status = AsyncMock(return_value={"state": state})
    return d


class ResumeTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    async def test_playing_station_is_noted_persisted_and_resumed_once(self):
        m = build(self.tmp.name)
        m._mpd_instances[A] = old = mpd(playing=True)
        await m._stop_mpd(A, remember=True)
        old.stop.assert_awaited_once()
        with open(m.PLAYBACK_RESUME_FILE) as f:
            self.assertEqual(json.load(f)[A]["uri"], URI)

        # A new app process reads the file
        m2 = build(self.tmp.name)
        new = mpd()
        self.assertTrue(await m2._resume_playback(A, new))
        new.resume.assert_awaited_once_with(URI)
        self.assertFalse(await m2._resume_playback(A, mpd()))  # consumed

    async def test_deliberate_stops_are_not_resumed(self):
        m = build(self.tmp.name)
        m._mpd_instances[A] = mpd(playing=False)          # MPD was stopped
        await m._stop_mpd(A, remember=True)
        self.assertFalse(await m._resume_playback(A, mpd()))
        m._mpd_instances[A] = mpd(playing=True)
        await m._stop_mpd(A)                                # forget / MPD off: no remember
        self.assertFalse(await m._resume_playback(A, mpd()))

    async def test_a_playing_entry_is_cleared_by_a_later_stop(self):
        m = build(self.tmp.name)
        m._mpd_instances[A] = mpd(playing=True)
        await m._stop_mpd(A, remember=True)
        m._mpd_instances[A] = mpd(playing=False)
        await m._stop_mpd(A, remember=True)
        self.assertFalse(await m._resume_playback(A, mpd()))

    async def test_old_entries_and_already_playing_are_left_alone(self):
        m = build(self.tmp.name)
        m._resume_state()[A] = {"uri": URI, "at": time.time() - m.PLAYBACK_RESUME_MAX_AGE - 5}
        new = mpd()
        self.assertFalse(await m._resume_playback(A, new))
        new.resume.assert_not_called()
        m._resume_state()[A] = {"uri": URI, "at": time.time()}
        busy = mpd(state="play")                            # Home Assistant was quicker
        self.assertFalse(await m._resume_playback(A, busy))
        busy.resume.assert_not_called()

    async def test_dead_daemon_and_broken_file_are_harmless(self):
        m = build(self.tmp.name)
        with open(m.PLAYBACK_RESUME_FILE, "w") as f:
            f.write("{broken")
        m._mpd_instances[A] = mpd(running=False)
        await m._stop_mpd(A, remember=True)
        self.assertFalse(await m._resume_playback(A, mpd()))


class ShutdownRaceTest(unittest.IsolatedAsyncioTestCase):
    """The container's stop signal reaches the MPD processes at the same time
    as the app: by the time the app stops a daemon it may already be gone."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    async def test_station_noted_while_playing_survives_a_dead_daemon(self):
        m = build(self.tmp.name)
        live = mpd(playing=True)
        self.assertTrue(await m._observe_playback(A, live))  # periodic note
        m._mpd_instances[A] = mpd(running=False)            # already gone at stop
        await m._stop_mpd(A, remember=True)
        m2 = build(self.tmp.name)                            # next app start
        new = mpd()
        self.assertTrue(await m2._resume_playback(A, new))
        new.resume.assert_awaited_once_with(URI)

    async def test_observed_stop_clears_the_note(self):
        m = build(self.tmp.name)
        await m._observe_playback(A, mpd(playing=True))
        self.assertFalse(await m._observe_playback(A, mpd(playing=False)))
        self.assertIsNone(await m._observe_playback(A, mpd(running=False)))  # unknown: no change
        self.assertFalse(await m._resume_playback(A, mpd()))

    async def test_unchanged_station_is_not_rewritten(self):
        m = build(self.tmp.name)
        await m._observe_playback(A, mpd(playing=True))
        before = os.stat(m.PLAYBACK_RESUME_FILE).st_mtime_ns
        os.utime(m.PLAYBACK_RESUME_FILE, ns=(before - 10**9, before - 10**9))
        await m._observe_playback(A, mpd(playing=True))
        self.assertEqual(os.stat(m.PLAYBACK_RESUME_FILE).st_mtime_ns, before - 10**9)


if __name__ == "__main__":
    unittest.main()
