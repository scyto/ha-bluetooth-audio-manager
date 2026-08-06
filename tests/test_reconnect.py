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
from unittest.mock import MagicMock, patch

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


if __name__ == "__main__":
    unittest.main()
