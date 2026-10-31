"""Repeat mode for live radio, and the guard against a reconnect loop.

A radio server closing the connection made MPD log "player: played <url>"
and stop; the speaker stayed silent until an outside watchdog played again
(observed: 3 minutes).  Repeat re-opens the stream at once.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from bt_audio_manager.audio.mpd import MPDManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

PLAYED = '2026-10-31T22:22:08 player: played "http://streams.radiobob.de/x/mp3-192/mediaplayer/"'


class FakeStderr:
    def __init__(self, lines):
        self._lines = [l.encode() + b"\n" for l in lines]

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


def build():
    mpd = MPDManager(address="AA:BB:CC:DD:EE:FF", port=6601, speaker_name="Speaker")
    mpd._client = MagicMock()
    mpd._client.repeat = AsyncMock()
    mpd._client.single = AsyncMock()
    mpd._ensure_connected = AsyncMock()
    return mpd


class RepeatTest(unittest.IsolatedAsyncioTestCase):

    async def test_enable_repeat_sets_repeat_and_clears_single(self):
        mpd = build()
        self.assertTrue(await mpd.enable_repeat())
        mpd._client.repeat.assert_awaited_once_with(1)
        mpd._client.single.assert_awaited_once_with(0)

    async def test_played_lines_from_stderr_count_as_stream_ends(self):
        mpd = build()
        mpd._process = MagicMock()
        mpd._process.stderr = FakeStderr([PLAYED, "decoder: something else", PLAYED])
        await mpd._stream_stderr()
        self.assertEqual(len(mpd._stream_ends), 2)

    async def test_occasional_stream_ends_keep_repeat_on(self):
        mpd = build()
        for _ in range(mpd.REPEAT_GUARD_MAX_ENDS):
            mpd._on_stream_end()
        self.assertIsNone(mpd._repeat_guard_task)
        mpd._client.repeat.assert_not_called()

    async def test_too_many_ends_switch_repeat_off_then_back_on(self):
        mpd = build()
        mpd.REPEAT_GUARD_COOLDOWN = 0
        mpd._running = True
        mpd._process = MagicMock(returncode=None)
        for _ in range(mpd.REPEAT_GUARD_MAX_ENDS + 1):
            mpd._on_stream_end()
        self.assertIsNotNone(mpd._repeat_guard_task)
        await mpd._repeat_guard_task
        self.assertEqual(
            [c.args for c in mpd._client.repeat.await_args_list], [(0,), (1,)]
        )

    async def test_ends_outside_the_window_do_not_add_up(self):
        mpd = build()
        loop = asyncio.get_running_loop()
        for i in range(mpd.REPEAT_GUARD_MAX_ENDS + 3):
            with patch.object(loop, "time", return_value=i * (mpd.REPEAT_GUARD_WINDOW / 2)):
                mpd._on_stream_end()
        self.assertIsNone(mpd._repeat_guard_task)

    async def test_stop_cancels_a_pending_cooldown(self):
        mpd = build()
        mpd._repeat_guard_task = asyncio.ensure_future(asyncio.sleep(3600))
        task = mpd._repeat_guard_task
        await mpd.stop()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main()
