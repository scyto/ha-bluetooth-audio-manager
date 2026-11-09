"""Auto Reconnect must actually mean "don't reconnect".

The setting used to be honoured only on the disconnect path.  Startup went
through ``reconnect_all()``, which read the device store directly and created
reconnect tasks without consulting it — so every add-on update, HA restart or
host reboot seized back a speaker the user had deliberately left free for
another source.  That silently broke the whole point of turning the toggle off
(#281).

These tests assert the *decision*, not the Bluetooth behaviour: given the
setting and the service state, does a reconnect task get created?
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bt_audio_manager.manager import BluetoothAudioManager
from bt_audio_manager.reconnect import ReconnectService

ADDR = "AA:BB:CC:DD:EE:FF"
ADDR2 = "11:22:33:44:55:66"


def make_manager(*, auto_reconnect=True, stored=(ADDR,)):
    """A manager stub exposing only what ReconnectService touches."""
    mgr = MagicMock()
    mgr.config.auto_reconnect = auto_reconnect
    mgr.config.reconnect_interval_seconds = 30
    mgr.config.reconnect_max_backoff_seconds = 300
    mgr.store.auto_connect_devices = [{"address": a} for a in stored]
    mgr.store.get_device.return_value = {"address": ADDR, "auto_connect": True}
    mgr.managed_devices = {}
    return mgr


class ReconnectEnabledTest(unittest.TestCase):
    """The predicate both scheduling paths and the retry loop consult."""

    def test_requires_both_running_and_setting(self):
        cases = [
            (True, True, True),
            (True, False, False),   # user turned Auto Reconnect off
            (False, True, False),   # service stopped
            (False, False, False),
        ]
        for running, auto, expected in cases:
            with self.subTest(running=running, auto_reconnect=auto):
                svc = ReconnectService(make_manager(auto_reconnect=auto))
                svc._running = running
                self.assertIs(svc._reconnect_enabled(), expected)


class ReconnectAllTest(unittest.IsolatedAsyncioTestCase):
    """Startup reconnection — the path that ignored the setting."""

    async def _run_reconnect_all(self, *, auto_reconnect, stored=(ADDR,), running=True):
        """Call reconnect_all() with the retry loop stubbed out.

        Returns the addresses a reconnect task was created for.  The real
        _reconnect_loop sleeps for at least QUICK_RETRY_DELAY and talks to
        D-Bus, neither of which belongs in a unit test.
        """
        started: list[str] = []

        async def fake_loop(self_, address):
            started.append(address)

        mgr = make_manager(auto_reconnect=auto_reconnect, stored=stored)
        svc = ReconnectService(mgr)
        svc._running = running

        with patch.object(ReconnectService, "_reconnect_loop", new=fake_loop):
            await svc.reconnect_all()
            if svc._tasks:
                await asyncio.gather(*svc._tasks.values())
        return started

    async def test_does_not_reconnect_when_auto_reconnect_is_off(self):
        """The #281 regression: a restart must not seize the speaker back."""
        started = await self._run_reconnect_all(auto_reconnect=False)
        self.assertEqual(started, [])

    async def test_reconnects_every_stored_device_when_on(self):
        started = await self._run_reconnect_all(
            auto_reconnect=True, stored=(ADDR, ADDR2),
        )
        self.assertCountEqual(started, [ADDR, ADDR2])

    async def test_does_not_reconnect_before_the_service_starts(self):
        started = await self._run_reconnect_all(auto_reconnect=True, running=False)
        self.assertEqual(started, [])

    async def test_no_stored_devices_is_not_an_error(self):
        started = await self._run_reconnect_all(auto_reconnect=True, stored=())
        self.assertEqual(started, [])

    async def test_store_is_not_consulted_when_disabled(self):
        """Cheap proof the guard runs first, rather than filtering afterwards."""
        mgr = make_manager(auto_reconnect=False)
        type(mgr.store).auto_connect_devices = property(
            lambda _: self.fail("store must not be read when Auto Reconnect is off")
        )
        svc = ReconnectService(mgr)
        svc._running = True
        await svc.reconnect_all()


class CancelAllTest(unittest.IsolatedAsyncioTestCase):
    """Turning the setting off must stop work already under way.

    Checking the setting between attempts is not enough: a task can be sitting
    inside connect_device() for tens of seconds and would still seize the
    speaker after the user asked us to stop.
    """

    async def test_cancels_in_flight_tasks(self):
        mgr = make_manager()
        svc = ReconnectService(mgr)
        svc._running = True

        async def slow():
            await asyncio.sleep(60)

        task = asyncio.create_task(slow())
        svc._tasks[ADDR] = task
        await asyncio.sleep(0)  # let it start

        svc.cancel_all()
        self.assertEqual(svc._tasks, {})

        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_clears_the_status_banner(self):
        mgr = make_manager()
        svc = ReconnectService(mgr)
        svc._running = True
        svc.cancel_all()
        mgr._broadcast_status.assert_called_with("")

    async def test_is_safe_with_no_tasks(self):
        svc = ReconnectService(make_manager())
        svc._running = True
        svc.cancel_all()  # must not raise


class AbandonTest(unittest.IsolatedAsyncioTestCase):
    """A loop that gives up must not leave a spinner claiming it is still going.

    web/static/app.js keeps a non-empty status visible until it receives an
    empty one, so an abandoned loop that stays quiet strands the banner.
    """

    async def test_clears_status_when_nothing_else_is_retrying(self):
        mgr = make_manager()
        svc = ReconnectService(mgr)
        svc._abandon(ADDR)
        mgr._broadcast_status.assert_called_with("")

    async def test_drops_the_task(self):
        mgr = make_manager()
        svc = ReconnectService(mgr)
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        svc._tasks[ADDR] = done
        svc._abandon(ADDR)
        self.assertNotIn(ADDR, svc._tasks)

    async def test_keeps_status_when_another_device_is_still_retrying(self):
        """Clearing unconditionally would hide the other device's live banner."""
        mgr = make_manager()
        svc = ReconnectService(mgr)

        async def slow():
            await asyncio.sleep(60)

        other = asyncio.create_task(slow())
        svc._tasks[ADDR2] = other
        await asyncio.sleep(0)

        svc._abandon(ADDR)
        mgr._broadcast_status.assert_not_called()

        other.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await other


class HandleDisconnectTest(unittest.TestCase):
    """The disconnect path already honoured the setting — keep it that way."""

    def _handle(self, *, auto_reconnect, running=True):
        started: list[str] = []

        async def fake_loop(self_, address):
            started.append(address)

        mgr = make_manager(auto_reconnect=auto_reconnect)
        svc = ReconnectService(mgr)
        svc._running = running

        async def drive():
            with patch.object(ReconnectService, "_reconnect_loop", new=fake_loop):
                svc.handle_disconnect(ADDR)
                if svc._tasks:
                    await asyncio.gather(*svc._tasks.values())
            return started

        return asyncio.run(drive())

    def test_ignores_disconnect_when_auto_reconnect_is_off(self):
        self.assertEqual(self._handle(auto_reconnect=False), [])

    def test_schedules_reconnect_when_on(self):
        self.assertEqual(self._handle(auto_reconnect=True), [ADDR])

    def test_ignores_disconnect_when_not_running(self):
        self.assertEqual(self._handle(auto_reconnect=True, running=False), [])


class ExplicitConnectFailureTest(unittest.IsolatedAsyncioTestCase):
    """A failed explicit connect must restore, not cancel, automatic recovery.

    connect_device() cancels any pending reconnect before an explicit
    (UI/API/force-reconnect) attempt.  If that attempt failed, nothing ever
    rescheduled the device, so a force-reconnect against a powered-off
    speaker left it without auto-reconnect until its next disconnect event.
    Based on Cayouche1989/ha-bluetooth-audio-manager@2c38ea0.
    """

    @staticmethod
    def _manager(*, auto_reconnect, side_effect=OSError("offline")):
        manager = BluetoothAudioManager.__new__(BluetoothAudioManager)
        manager.config = MagicMock(auto_reconnect=auto_reconnect)
        manager.reconnect_service = MagicMock()
        manager._connecting = set()
        manager._suppress_reconnect = set()
        manager._suppress_reconnect_at = {}
        manager.managed_devices = {}
        manager.event_bus = MagicMock()
        manager._broadcast_status = MagicMock()
        manager._get_or_create_device = AsyncMock(side_effect=side_effect)
        return manager

    async def test_failed_explicit_connect_restores_retry_when_enabled(self):
        manager = self._manager(auto_reconnect=True)

        with self.assertRaises(OSError):
            await manager.connect_device(ADDR)

        manager.reconnect_service.cancel_reconnect.assert_called_once_with(ADDR)
        manager.reconnect_service.handle_disconnect.assert_called_once_with(ADDR)

    async def test_failed_explicit_connect_does_not_retry_when_disabled(self):
        manager = self._manager(auto_reconnect=False)

        with self.assertRaises(OSError):
            await manager.connect_device(ADDR)

        manager.reconnect_service.handle_disconnect.assert_not_called()

    async def test_failure_inside_reconnect_loop_does_not_reschedule(self):
        # The reconnect loop owns its own backoff — rescheduling from inside
        # it would spawn a second loop for the same device.
        manager = self._manager(auto_reconnect=True)

        with self.assertRaises(OSError):
            await manager.connect_device(ADDR, _from_reconnect=True)

        manager.reconnect_service.handle_disconnect.assert_not_called()


class UserDisconnectFlagTest(unittest.TestCase):
    """The "user-initiated" flag must not outlive the disconnect it was for (#317).

    It used to be consumed only by the *next* disconnect event.  When that
    event never came, the flag lingered until an unrelated drop much later,
    which was then skipped as "user-initiated" and never reconnected.
    """

    def _manager(self):
        manager = BluetoothAudioManager.__new__(BluetoothAudioManager)
        manager._suppress_reconnect = set()
        manager._suppress_reconnect_at = {}
        manager._connecting = set()
        manager.reconnect_service = MagicMock()
        for attr in ("_device_connect_time", "_last_signaled_volume", "_last_pa_volume",
                     "_connected_rssi", "_rssi_timestamp", "_recent_disconnects"):
            setattr(manager, attr, {})
        manager._running_sinks = set()
        manager._adapter_path = "/org/bluez/hci0"
        manager._fire_and_forget = MagicMock(side_effect=lambda coro: coro.close())
        manager._broadcast_all = MagicMock()
        return manager

    def _disconnect_at(self, manager, t):
        with patch("bt_audio_manager.manager.time.monotonic", return_value=t):
            manager._on_device_disconnected(ADDR)

    def _flag_at(self, manager, t):
        with patch("bt_audio_manager.manager.time.monotonic", return_value=t):
            manager._suppress_auto_reconnect(ADDR)

    def test_fresh_flag_suppresses_reconnect(self):
        manager = self._manager()
        self._flag_at(manager, 100.0)
        self._disconnect_at(manager, 105.0)
        manager.reconnect_service.handle_disconnect.assert_not_called()
        self.assertNotIn(ADDR, manager._suppress_reconnect)

    def test_stale_flag_is_ignored_and_reconnect_scheduled(self):
        manager = self._manager()
        self._flag_at(manager, 100.0)
        self._disconnect_at(manager, 100.0 + 31)
        manager.reconnect_service.handle_disconnect.assert_called_once_with(ADDR)
        self.assertNotIn(ADDR, manager._suppress_reconnect)
        self.assertNotIn(ADDR, manager._suppress_reconnect_at)

    def test_flag_is_consumed_by_one_disconnect_only(self):
        manager = self._manager()
        self._flag_at(manager, 100.0)
        self._disconnect_at(manager, 101.0)
        self._disconnect_at(manager, 200.0)  # a later, natural drop
        manager.reconnect_service.handle_disconnect.assert_called_once_with(ADDR)

    def test_untimed_flag_still_suppresses(self):
        # The HFP cycle sets and clears the flag around its own bounded work.
        manager = self._manager()
        manager._suppress_reconnect.add(ADDR)
        self._disconnect_at(manager, 10_000.0)
        manager.reconnect_service.handle_disconnect.assert_not_called()

    def test_no_flag_reconnects(self):
        manager = self._manager()
        self._disconnect_at(manager, 100.0)
        manager.reconnect_service.handle_disconnect.assert_called_once_with(ADDR)


if __name__ == "__main__":
    unittest.main()
