"""PulseAudio going away must not need a manual app restart.

PulseAudio lives in HAOS's audio plugin, a container of its own.  It can be
restarted without this app (``ha audio restart``, a plugin update, a crash)
and may still be starting when the app starts.  Both used to be final: the
client was never (re)connected, every sink lookup failed from then on, and
the only trace was a debug line every 5 s.
"""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from bt_audio_manager import manager as manager_module
    from bt_audio_manager.audio import pulse as pulse_module
    from bt_audio_manager.audio.pulse import PulseAudioManager
    from bt_audio_manager.manager import BluetoothAudioManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

from .logcapture import warnings_from

ADDR = "AA:BB:CC:DD:EE:FF"


async def yield_once():
    """Let other tasks run (asyncio.sleep is patched out in these tests)."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    loop.call_soon(done.set_result, None)
    await done


class FakePulseAsync:
    """Stands in for pulsectl_asyncio.PulseAsync; *outcomes* is consumed per connect()."""

    outcomes: list = []
    created: list = []

    def __init__(self, name):
        self.connected = False
        self.closed = False
        FakePulseAsync.created.append(self)

    async def connect(self):
        await yield_once()
        outcome = FakePulseAsync.outcomes.pop(0) if FakePulseAsync.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        self.connected = True

    def close(self):
        self.closed = True
        self.connected = False


class PulseClientTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        FakePulseAsync.outcomes, FakePulseAsync.created = [], []
        for p in (
            patch.object(pulse_module, "PulseAsync", FakePulseAsync),
            patch.dict(os.environ, {"PULSE_SERVER": "unix:/run/audio/pulse.sock"}),
            patch.object(pulse_module.asyncio, "sleep", AsyncMock()),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.pa = PulseAudioManager()
        self.pa.start_event_monitor = AsyncMock()

    async def test_connected_follows_the_connection(self):
        self.assertFalse(self.pa.connected)
        await self.pa.connect()
        self.assertTrue(self.pa.connected)
        self.pa._pulse.connected = False  # what pulsectl does when PulseAudio exits
        self.assertFalse(self.pa.connected)

    async def test_a_failed_connect_keeps_no_client(self):
        FakePulseAsync.outcomes = [RuntimeError("Failed to connect to pulseaudio server")]
        with self.assertRaises(RuntimeError):
            await self.pa.connect()
        self.assertIsNone(self.pa._pulse)
        self.assertTrue(FakePulseAsync.created[0].closed)

    async def test_reconnect_replaces_the_client_and_forgets_indices(self):
        await self.pa.connect()
        old = self.pa._pulse
        self.pa._spec_cache = {"bluez_sink.x.a2dp_sink": {"rate": 44100}}
        self.pa._spec_sink_index = {"bluez_sink.x.a2dp_sink": 7}
        self.pa._spec_failed_at = 1.0
        self.pa._pin_history = {12: [1.0]}
        await self.pa.reconnect(retries=1)
        self.assertTrue(old.closed)
        self.assertIsNot(self.pa._pulse, old)
        self.assertTrue(self.pa.connected)
        self.assertEqual(
            (self.pa._spec_cache, self.pa._spec_sink_index, self.pa._spec_failed_at, self.pa._pin_history),
            ({}, {}, None, {}),
        )
        self.pa.start_event_monitor.assert_awaited()

    async def test_reconnect_retries_then_gives_up(self):
        FakePulseAsync.outcomes = [RuntimeError("down")] * 3
        with self.assertRaises(ConnectionError):
            await self.pa.reconnect(retries=3, delay=0)
        self.assertEqual(len(FakePulseAsync.created), 3)
        self.assertFalse(self.pa.connected)

    async def test_two_reconnects_at_once_connect_only_once(self):
        await self.pa.connect()
        self.pa._pulse.connected = False
        await asyncio.gather(self.pa.reconnect(retries=1), self.pa.reconnect(retries=1))
        self.assertEqual(len(FakePulseAsync.created), 2)  # the first connect + one reconnect


def build_manager(pulse=None):
    m = BluetoothAudioManager.__new__(BluetoothAudioManager)
    m.pulse = pulse
    m.event_bus = MagicMock()
    m.managed_devices = {}
    m._connecting = set()
    m._a2dp_attempts = {}
    m._pulse_failures = 0
    m._pulse_down_since = None
    m._pulse_retry_at = 0.0
    m._pulse_reported_at = 0.0
    m._pulse_restore_task = None
    m._restore_audio_after_pulse_return = AsyncMock()
    return m


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class PulseReadyTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.clock = Clock()
        p = patch.object(manager_module.time, "monotonic", self.clock.monotonic)
        p.start()
        self.addCleanup(p.stop)

    async def settle(self, m):
        if m._pulse_restore_task:
            await m._pulse_restore_task

    async def test_healthy_connection_is_left_alone(self):
        pulse = MagicMock(connected=True, reconnect=AsyncMock())
        m = build_manager(pulse)
        self.assertTrue(await m._pulse_ready())
        pulse.reconnect.assert_not_called()
        m._restore_audio_after_pulse_return.assert_not_called()

    async def test_lost_connection_is_reconnected_and_audio_restored(self):
        pulse = MagicMock(connected=False, reconnect=AsyncMock())
        m = build_manager(pulse)
        with warnings_from(manager_module.logger) as logs:
            self.assertTrue(await m._pulse_ready())
        await self.settle(m)
        pulse.reconnect.assert_awaited_once_with(retries=1)
        m._restore_audio_after_pulse_return.assert_awaited_once()
        self.assertIsNone(m._pulse_down_since)
        self.assertIn("connection lost", logs[0])
        self.assertIn("reachable again", logs[-1])

    async def test_retries_are_spaced_while_pulseaudio_stays_down(self):
        pulse = MagicMock(connected=False, reconnect=AsyncMock(side_effect=ConnectionError("down")))
        m = build_manager(pulse)
        self.assertFalse(await m._pulse_ready())           # attempt 1
        self.clock.now += m.SINK_POLL_INTERVAL
        self.assertFalse(await m._pulse_ready())           # too early: no attempt
        self.assertEqual(pulse.reconnect.await_count, 1)
        self.clock.now += m.PULSE_RETRY_INTERVAL
        self.assertFalse(await m._pulse_ready())           # attempt 2
        self.assertEqual(pulse.reconnect.await_count, 2)
        m._restore_audio_after_pulse_return.assert_not_called()
        # ... and once it is back, the outage ends
        pulse.reconnect.side_effect = None
        self.clock.now += m.PULSE_RETRY_INTERVAL
        self.assertTrue(await m._pulse_ready())
        await self.settle(m)
        m._restore_audio_after_pulse_return.assert_awaited_once()

    async def test_a_long_outage_is_reported_now_and_then(self):
        pulse = MagicMock(connected=False, reconnect=AsyncMock(side_effect=ConnectionError("down")))
        m = build_manager(pulse)
        with warnings_from(manager_module.logger) as logs:
            for _ in range(70):  # ~12 min in 10 s steps
                await m._pulse_ready()
                self.clock.now += m.PULSE_RETRY_INTERVAL
        still = [line for line in logs if "still not reachable" in line]
        self.assertEqual(len(still), 2)
        self.assertEqual(sum("connection lost" in line for line in logs), 1)

    async def test_unreachable_at_start_is_connected_later(self):
        m = build_manager(None)
        m._pulse_down_since = m._pulse_reported_at = self.clock.now  # as start() leaves it
        m._connect_pulse = AsyncMock(side_effect=[False, True])
        self.assertFalse(await m._pulse_ready())
        self.clock.now += m.PULSE_RETRY_INTERVAL
        self.assertTrue(await m._pulse_ready())
        await self.settle(m)
        self.assertEqual(m._connect_pulse.await_count, 2)
        m._restore_audio_after_pulse_return.assert_awaited_once()

    async def test_a_connection_that_keeps_failing_polls_is_replaced(self):
        pulse = MagicMock(connected=True, reconnect=AsyncMock())
        m = build_manager(pulse)
        m._pulse_failures = m.PULSE_FAILURES_BEFORE_RECONNECT - 1
        self.assertTrue(await m._pulse_ready())
        pulse.reconnect.assert_not_called()
        m._pulse_failures = m.PULSE_FAILURES_BEFORE_RECONNECT
        self.assertTrue(await m._pulse_ready())
        pulse.reconnect.assert_awaited_once()
        self.assertEqual(m._pulse_failures, 0)
        await self.settle(m)


class ConnectPulseTest(unittest.IsolatedAsyncioTestCase):

    async def test_success_wires_the_callbacks(self):
        m = build_manager(None)
        fake = MagicMock(connect=AsyncMock(), start_event_monitor=AsyncMock())
        with patch.object(manager_module, "PulseAudioManager", return_value=fake):
            self.assertTrue(await m._connect_pulse())
        self.assertIs(m.pulse, fake)
        fake.on_volume_change.assert_called_once()
        fake.on_sink_state_change.assert_called_once()
        fake.on_sink_idle.assert_called_once()
        fake.set_stream_owner_resolver.assert_called_once()
        fake.set_volume_lock_resolver.assert_called_once()
        fake.start_event_monitor.assert_awaited_once()

    async def test_failure_leaves_no_client(self):
        m = build_manager(None)
        fake = MagicMock(connect=AsyncMock(side_effect=ConnectionError("not reachable")),
                         disconnect=AsyncMock())
        with patch.object(manager_module, "PulseAudioManager", return_value=fake):
            self.assertFalse(await m._connect_pulse())
        self.assertIsNone(m.pulse)
        fake.disconnect.assert_awaited_once()

    async def test_a_hanging_connect_is_given_up(self):
        m = build_manager(None)
        m.PULSE_CONNECT_TIMEOUT = 0.05

        async def hang():
            await asyncio.Event().wait()

        fake = MagicMock(connect=hang, disconnect=AsyncMock())
        with patch.object(manager_module, "PulseAudioManager", return_value=fake):
            self.assertFalse(await asyncio.wait_for(m._connect_pulse(), timeout=2))


class RestoreTest(unittest.IsolatedAsyncioTestCase):

    def build(self, *, connected=True, sinks):
        """*sinks*: what get_sink_for_address returns on successive calls."""
        pulse = MagicMock(get_sink_for_address=AsyncMock(side_effect=list(sinks)))
        m = build_manager(pulse)
        del m._restore_audio_after_pulse_return  # use the real one
        m.PULSE_RESTORE_SETTLE = 0
        m.managed_devices = {ADDR: MagicMock(is_connected=AsyncMock(return_value=connected))}
        m._on_device_connected_async = AsyncMock()
        m.force_reconnect_device = AsyncMock()
        return m

    async def test_speaker_that_kept_its_sink_is_left_alone(self):
        m = self.build(sinks=["bluez_sink.x.a2dp_sink"])
        await m._restore_audio_after_pulse_return()
        m._on_device_connected_async.assert_not_called()
        m.force_reconnect_device.assert_not_called()

    async def test_disconnected_speaker_is_left_to_the_reconnect_path(self):
        m = self.build(connected=False, sinks=[])
        await m._restore_audio_after_pulse_return()
        m._on_device_connected_async.assert_not_called()

    async def test_connected_speaker_without_sink_is_set_up_again(self):
        m = self.build(sinks=[None, "bluez_sink.x.a2dp_sink"])
        m._a2dp_attempts[ADDR] = 3  # an earlier give-up must not block this
        await m._restore_audio_after_pulse_return()
        m._on_device_connected_async.assert_awaited_once_with(ADDR)
        m.force_reconnect_device.assert_not_called()
        self.assertNotIn(ADDR, m._a2dp_attempts)

    async def test_last_resort_is_a_forced_reconnect(self):
        m = self.build(sinks=[None, None])
        await m._restore_audio_after_pulse_return()
        m.force_reconnect_device.assert_awaited_once_with(ADDR)

    async def test_one_speaker_failing_does_not_stop_the_others(self):
        other = "11:22:33:44:55:66"
        m = self.build(sinks=[None, None, None, "bluez_sink.y.a2dp_sink"])
        m.managed_devices[other] = MagicMock(is_connected=AsyncMock(return_value=True))
        m.force_reconnect_device = AsyncMock(side_effect=RuntimeError("boom"))
        await m._restore_audio_after_pulse_return()
        self.assertEqual(
            [c.args[0] for c in m._on_device_connected_async.await_args_list], [ADDR, other],
        )


class SinkPollTest(unittest.IsolatedAsyncioTestCase):
    """The poll itself: counts failures, keeps running, self-heals when fine."""

    async def run_poll(self, m, rounds):
        calls = {"n": 0}

        async def fake_sleep(_):
            calls["n"] += 1
            if calls["n"] > rounds:
                raise asyncio.CancelledError

        with patch.object(manager_module.asyncio, "sleep", fake_sleep):
            await asyncio.wait_for(m._sink_poll_loop(), timeout=2)

    def build(self, list_bt_sinks):
        pulse = MagicMock(connected=True, list_bt_sinks=list_bt_sinks, reconnect=AsyncMock())
        m = build_manager(pulse)
        m._last_sink_snapshot = ""
        m._poll_running_sinks = set()
        m._self_heal_mpd = AsyncMock()
        return m

    async def test_failed_polls_lead_to_a_reconnect(self):
        n = BluetoothAudioManager.PULSE_FAILURES_BEFORE_RECONNECT
        m = self.build(AsyncMock(side_effect=[RuntimeError("Operation failed")] * n + [[]]))
        with warnings_from(manager_module.logger) as logs:
            await self.run_poll(m, n + 1)
        m.pulse.reconnect.assert_awaited_once()
        m._self_heal_mpd.assert_awaited_once_with([])  # polling again after the reconnect
        # one visible line per streak, not one every 5 s
        self.assertEqual(sum("Sink poll failed" in line for line in logs), 1)
        if m._pulse_restore_task:
            await m._pulse_restore_task

    async def test_a_hanging_poll_counts_as_failure(self):
        async def hang():
            await asyncio.Event().wait()

        m = self.build(hang)
        m.SINK_POLL_TIMEOUT = 0.02
        await self.run_poll(m, 2)
        self.assertEqual(m._pulse_failures, 2)

    async def test_good_poll_resets_the_count_and_heals_mpd(self):
        sinks = [{"name": "bluez_sink.x.a2dp_sink", "state": "running"}]
        m = self.build(AsyncMock(side_effect=[RuntimeError("once"), sinks]))
        await self.run_poll(m, 2)
        self.assertEqual(m._pulse_failures, 0)
        m._self_heal_mpd.assert_awaited_once_with(sinks)
        self.assertEqual(m._poll_running_sinks, {"bluez_sink.x.a2dp_sink"})

    async def test_nothing_is_polled_while_pulseaudio_is_down(self):
        m = self.build(AsyncMock())
        m.pulse.connected = False
        m.pulse.reconnect = AsyncMock(side_effect=ConnectionError("down"))
        await self.run_poll(m, 3)
        m.pulse.list_bt_sinks.assert_not_called()


class AudioRestartTest(unittest.IsolatedAsyncioTestCase):
    """_restart_audio_service must not ask for a second restart on a client error."""

    async def run_restart(self, m):
        posts = []

        class Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def text(self):
                return ""

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, url, **kw):
                posts.append(url)
                return Resp()

        import aiohttp
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "t"}), \
             patch.object(aiohttp, "ClientSession", Session), \
             patch.object(manager_module.asyncio, "sleep", AsyncMock()):
            await m._restart_audio_service()
        return posts

    async def test_no_client_yet(self):
        m = build_manager(None)
        self.assertEqual(len(await self.run_restart(m)), 1)

    async def test_reconnect_error_does_not_restart_twice(self):
        m = build_manager(MagicMock(reconnect=AsyncMock(side_effect=RuntimeError("odd"))))
        self.assertEqual(len(await self.run_restart(m)), 1)


if __name__ == "__main__":
    unittest.main()
