"""MPD helpers must survive odd input and one-off errors.

Each of these used to end a background task for good, with nothing in the
log at the default level:

* the stderr reader — after which nobody drains MPD's log pipe, so MPD
  blocks on a log line once the pipe is full and hangs with a live process;
* the playback watch — which carries the volume lock and the retry after a
  stream error for that speaker.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import mpd as mpdlib
    from bt_audio_manager.audio import mpd as mpd_module
    from bt_audio_manager.audio.mpd import MPDManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

PLAYED = b'2026-11-10T02:20:14 player: played "http://example.invalid/stream"\n'


class ScriptedStderr:
    """readline() returns the scripted items; an exception item is raised."""

    def __init__(self, items):
        self._items = list(items)

    async def readline(self):
        if not self._items:
            return b""
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def build():
    m = MPDManager(address="AA:BB:CC:DD:EE:FF", port=6601, speaker_name="Speaker")
    m._on_stream_end = MagicMock()
    return m


class StderrReaderTest(unittest.IsolatedAsyncioTestCase):

    async def read(self, items):
        m = build()
        m._process = MagicMock(stderr=ScriptedStderr(items))
        await asyncio.wait_for(m._stream_stderr(), timeout=2)
        return m

    async def test_invalid_utf8_does_not_stop_the_reader(self):
        m = await self.read([b"curl: title \xe4\xf6\xfc \xff\n", PLAYED])
        m._on_stream_end.assert_called_once()

    async def test_over_long_line_is_skipped(self):
        m = await self.read([ValueError("Separator is not found, and chunk exceed the limit"), PLAYED])
        m._on_stream_end.assert_called_once()

    async def test_error_in_line_handling_does_not_stop_the_reader(self):
        m = build()
        m._on_stream_end = MagicMock(side_effect=[RuntimeError("boom"), None])
        m._process = MagicMock(stderr=ScriptedStderr([PLAYED, PLAYED]))
        await asyncio.wait_for(m._stream_stderr(), timeout=2)
        self.assertEqual(m._on_stream_end.call_count, 2)

    async def test_ends_at_eof_and_on_a_broken_pipe(self):
        await self.read([])  # EOF at once
        m = await self.read([OSError("broken"), PLAYED])
        m._on_stream_end.assert_not_called()


class FakeClient:
    """Stands in for mpd.asyncio.MPDClient; *behaviour* is consumed per instance."""

    behaviour: list = []
    created: list = []

    def __init__(self):
        self.outcome = FakeClient.behaviour.pop(0) if FakeClient.behaviour else None
        self.disconnected = False
        FakeClient.created.append(self)

    async def connect(self, host, port):
        if isinstance(self.outcome, BaseException):
            raise self.outcome

    async def password(self, _):
        return None

    def disconnect(self):
        self.disconnected = True


class ConnectClientTest(unittest.IsolatedAsyncioTestCase):

    async def connect(self, behaviour):
        FakeClient.behaviour, FakeClient.created = list(behaviour), []
        m = build()
        with patch.object(mpd_module, "MPDClient", FakeClient), \
             patch.object(mpd_module.asyncio, "sleep", AsyncMock()):
            await m._connect_client()
        return m

    async def test_library_errors_do_not_escape(self):
        # python-mpd2's own ConnectionError is not an OSError
        self.assertFalse(issubclass(mpdlib.ConnectionError, OSError))
        m = await self.connect([mpdlib.ConnectionError("No response from server while reading MPD hello")] * 5)
        self.assertIsNone(m._client)
        self.assertEqual(len(FakeClient.created), 5)
        self.assertTrue(all(c.disconnected for c in FakeClient.created))

    async def test_a_later_attempt_can_succeed(self):
        m = await self.connect([ConnectionRefusedError(), mpdlib.ProtocolError("bad hello"), None])
        self.assertIs(m._client, FakeClient.created[2])
        self.assertFalse(FakeClient.created[2].disconnected)


class WatchTest(unittest.IsolatedAsyncioTestCase):

    async def run_watch(self, m, rounds):
        """Run the watch until *rounds* get_status calls were made."""
        calls = {"n": 0}
        script = m._script

        async def get_status():
            calls["n"] += 1
            if calls["n"] >= rounds:
                m._running = False
            item = script.pop(0) if script else {"state": "play"}
            if isinstance(item, BaseException):
                raise item
            return item

        m.get_status = get_status
        m._running = True
        with patch.object(mpd_module.asyncio, "sleep", AsyncMock()):
            await asyncio.wait_for(m._watch_playback(), timeout=2)
        return calls["n"]

    async def test_one_failing_round_does_not_end_the_watch(self):
        m = build()
        m.volume_lock, m.volume_lock_level = True, 80
        m.set_volume = AsyncMock()
        m._script = [mpdlib.ConnectionError("lost"), RuntimeError("odd"), {"state": "play", "volume": "100"}]
        rounds = await self.run_watch(m, 3)
        self.assertEqual(rounds, 3)
        m.set_volume.assert_awaited_once_with(80)  # the lock still works afterwards

    async def test_watch_ends_once_the_daemon_has_exited(self):
        m = build()
        m._process = MagicMock(returncode=1)
        m._script = []
        m.get_status = AsyncMock()
        m._running = True
        with patch.object(mpd_module.asyncio, "sleep", AsyncMock()):
            await asyncio.wait_for(m._watch_playback(), timeout=2)
        m.get_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
