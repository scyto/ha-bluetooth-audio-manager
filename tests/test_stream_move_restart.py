"""A stream moved back to its own speaker gets its MPD output re-opened.

13.11.2026: when 87.5 reconnected, PulseAudio moved the 97.0 stream onto it.
The add-on moved it back within milliseconds, but the 97.0 speaker then got
only digital silence (gap monitor: "silence before Bluetooth") while Home
Assistant showed "playing"; a stop/play fixed it.  The manager now does that
stop/play itself after every move back.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from bt_audio_manager.manager import BluetoothAudioManager

ADDR = "41:42:00:00:00:01"


def make_manager(state="play", running=True):
    m = BluetoothAudioManager.__new__(BluetoothAudioManager)
    mpd = MagicMock()
    mpd.is_running = running
    mpd.get_status = AsyncMock(return_value={"state": state})
    mpd.restart_playback = AsyncMock(return_value=True)
    m._mpd_instances = {ADDR: mpd}
    m.STREAM_MOVE_RESTART_DELAY = 0
    return m, mpd


class StreamMoveRestartTest(unittest.IsolatedAsyncioTestCase):

    async def _run(self, m):
        m._on_mpd_stream_moved_back(ADDR)
        await m._stream_restart_tasks[ADDR]

    async def test_playing_stream_is_reopened(self):
        m, mpd = make_manager()
        await self._run(m)
        mpd.restart_playback.assert_awaited_once()

    async def test_stopped_player_is_left_alone(self):
        m, mpd = make_manager(state="stop")
        await self._run(m)
        mpd.restart_playback.assert_not_awaited()

    async def test_mpd_not_running(self):
        m, mpd = make_manager(running=False)
        await self._run(m)
        mpd.restart_playback.assert_not_awaited()

    async def test_two_moves_in_a_row_restart_once(self):
        m, mpd = make_manager()
        m.STREAM_MOVE_RESTART_DELAY = 0.05
        m._on_mpd_stream_moved_back(ADDR)
        m._on_mpd_stream_moved_back(ADDR)
        await m._stream_restart_tasks[ADDR]
        mpd.restart_playback.assert_awaited_once()

    async def test_unknown_speaker(self):
        m, mpd = make_manager()
        m._on_mpd_stream_moved_back("41:42:00:00:00:99")
        await m._stream_restart_tasks["41:42:00:00:00:99"]
        mpd.restart_playback.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
