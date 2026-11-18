"""pactl calls are time-limited.

``pactl list sinks`` runs inside the 5 s sink poll, which also carries the
MPD self-heal.  A PulseAudio that accepts the connection but never answers
left ``proc.communicate()`` waiting for good — and the poll with it.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

try:
    from bt_audio_manager.audio import pulse as pulse_module
    from bt_audio_manager.audio.pulse import PulseAudioManager, run_pactl
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc


class FakeProc:
    def __init__(self, *, hang=False, returncode=0, stdout=b"", stderr=b""):
        self.hang, self.returncode = hang, returncode
        self._out, self._err = stdout, stderr
        self.killed = False

    async def communicate(self):
        if self.hang:
            await asyncio.Event().wait()
        return self._out, self._err

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def spawn(proc):
    return patch.object(pulse_module.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))


class RunPactlTest(unittest.IsolatedAsyncioTestCase):

    async def test_result_is_passed_through(self):
        with spawn(FakeProc(returncode=3, stdout=b"out", stderr=b"err")) as exec_mock:
            self.assertEqual(await run_pactl("list", "sinks"), (3, b"out", b"err"))
        self.assertEqual(exec_mock.await_args.args, ("pactl", "list", "sinks"))

    async def test_hanging_pactl_is_killed(self):
        proc = FakeProc(hang=True)
        with spawn(proc):
            result = await asyncio.wait_for(run_pactl("list", "sinks", timeout=0.05), timeout=2)
        self.assertEqual(result, (None, b"", b""))
        self.assertTrue(proc.killed)

    async def test_cancellation_kills_pactl_and_propagates(self):
        proc = FakeProc(hang=True)
        with spawn(proc):
            task = asyncio.ensure_future(run_pactl("list", "sinks", timeout=30))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(proc.killed)


class CallersTest(unittest.IsolatedAsyncioTestCase):

    async def test_sample_specs_are_empty_after_a_timeout(self):
        with patch.object(pulse_module, "run_pactl", AsyncMock(return_value=(None, b"", b""))):
            self.assertEqual(await PulseAudioManager()._pactl_sample_specs(), {})

    async def test_set_sink_volume_reports_failure_after_a_timeout(self):
        with patch.object(pulse_module, "run_pactl", AsyncMock(return_value=(None, b"", b""))):
            self.assertFalse(await PulseAudioManager().set_sink_volume("bluez_sink.x.a2dp_sink", 100))

    async def test_set_sink_volume_success(self):
        with patch.object(pulse_module, "run_pactl", AsyncMock(return_value=(0, b"", b""))) as run:
            self.assertTrue(await PulseAudioManager().set_sink_volume("bluez_sink.x.a2dp_sink", 150))
        self.assertEqual(run.await_args.args, ("set-sink-volume", "bluez_sink.x.a2dp_sink", "100%"))


if __name__ == "__main__":
    unittest.main()
