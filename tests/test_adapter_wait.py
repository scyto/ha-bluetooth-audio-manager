"""A configured adapter that is missing at startup is waited for.

The app used to fall back to "the first powered adapter" for the session.
With two adapters in the host that is the one Home Assistant uses itself:
the speakers are not paired there, and the app stayed on it — the
adapter-return watch follows the adapter the app is bound to, so the
configured stick showing up a few seconds later changed nothing.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from bt_audio_manager import __main__ as main_module
    from bt_audio_manager import manager as manager_module
    from bt_audio_manager.manager import BluetoothAudioManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

from .logcapture import warnings_from

OURS = "00:11:22:33:44:55"
OTHER = "66:77:88:99:AA:BB"


def adapter(mac, name, powered=True):
    return {"path": f"/org/bluez/{name}", "name": name, "address": mac, "powered": powered}


class Config:
    def __init__(self, bt_adapter):
        self.bt_adapter = bt_adapter
        self.saved = False

    @property
    def bt_adapter_is_mac(self):
        return ":" in self.bt_adapter

    def save_settings(self):
        self.saved = True


def build(bt_adapter):
    m = BluetoothAudioManager.__new__(BluetoothAudioManager)
    m.config = Config(bt_adapter)
    m.bus = MagicMock()
    m.event_bus = MagicMock()
    return m


class AdapterWaitTest(unittest.IsolatedAsyncioTestCase):

    async def resolve(self, m, listings, on_sleep=None):
        """Resolve with BluezAdapter.list_all returning *listings* in turn."""
        listings = list(listings)

        async def list_all(_bus):
            item = listings.pop(0) if len(listings) > 1 else listings[0]
            if isinstance(item, BaseException):
                raise item
            return item

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if on_sleep:
                on_sleep(len(sleeps))

        with patch.object(manager_module.BluezAdapter, "list_all", list_all), \
             patch.object(manager_module.asyncio, "sleep", fake_sleep):
            path = await asyncio.wait_for(m._resolve_adapter_path(), timeout=2)
        return path, sleeps

    async def test_present_adapter_resolves_at_once(self):
        m = build(OURS)
        path, sleeps = await self.resolve(m, [[adapter(OTHER, "hci0"), adapter(OURS, "hci1")]])
        self.assertEqual((path, sleeps), ("/org/bluez/hci1", []))

    async def test_mac_is_compared_case_insensitively(self):
        m = build(OURS.lower())
        path, _ = await self.resolve(m, [[adapter(OURS, "hci1")]])
        self.assertEqual(path, "/org/bluez/hci1")

    async def test_missing_adapter_is_waited_for_not_replaced(self):
        m = build(OURS)
        only_other = [adapter(OTHER, "hci0")]
        with warnings_from(manager_module.logger) as logs:
            path, sleeps = await self.resolve(
                m, [only_other, only_other, RuntimeError("bluetoothd restarting"),
                    [adapter(OTHER, "hci0"), adapter(OURS, "hci1")]],
            )
        self.assertEqual(path, "/org/bluez/hci1")  # never hci0
        # three looks, then a moment for the fresh adapter to power up
        self.assertEqual(sleeps, [m.ADAPTER_WAIT_INTERVAL] * 3 + [m.ADAPTER_RETURN_SETTLE])
        self.assertIn("waiting for it", logs[0])
        self.assertIn("appeared as /org/bluez/hci1", logs[-1])
        statuses = [c.args[1]["message"] for c in m.event_bus.emit.call_args_list]
        self.assertIn("waiting for it", statuses[0])
        self.assertEqual(statuses[-1], "")

    async def test_a_changed_setting_ends_the_wait(self):
        m = build(OURS)

        def choose_other(n):
            if n == 2:
                m.config.bt_adapter = OTHER  # picked in the UI while waiting

        path, sleeps = await self.resolve(m, [[adapter(OTHER, "hci0")]], on_sleep=choose_other)
        self.assertEqual(path, "/org/bluez/hci0")
        self.assertEqual(len(sleeps), 2)

    async def test_switching_to_auto_ends_the_wait(self):
        m = build(OURS)

        def choose_auto(n):
            m.config.bt_adapter = "auto"

        path, _ = await self.resolve(m, [[adapter(OTHER, "hci0")]], on_sleep=choose_auto)
        self.assertEqual(path, "/org/bluez/hci0")

    async def test_a_long_wait_is_reported_now_and_then(self):
        m = build(OURS)
        clock = {"now": 0.0}

        def tick(n):
            clock["now"] += m.ADAPTER_WAIT_INTERVAL
            if n == 150:  # 12.5 min
                m.config.bt_adapter = "auto"

        with patch.object(manager_module.time, "monotonic", lambda: clock["now"]), \
             warnings_from(manager_module.logger) as logs:
            await self.resolve(m, [[adapter(OTHER, "hci0")]], on_sleep=tick)
        still = [line for line in logs if "Still waiting" in line]
        self.assertEqual(len(still), 2)
        self.assertIn(f"hci0 {OTHER}", still[0])


class StartOrStopTest(unittest.IsolatedAsyncioTestCase):
    """A stop signal must end a startup that is still waiting."""

    async def test_finished_startup(self):
        stop = asyncio.ensure_future(asyncio.Event().wait())
        self.assertTrue(await main_module.start_or_stop(AsyncMock()(), stop))
        stop.cancel()

    async def test_startup_error_is_raised(self):
        stop = asyncio.ensure_future(asyncio.Event().wait())
        with self.assertRaises(RuntimeError):
            await main_module.start_or_stop(AsyncMock(side_effect=RuntimeError("no D-Bus"))(), stop)
        stop.cancel()

    async def test_stop_signal_cancels_a_waiting_startup(self):
        shutdown = asyncio.Event()
        state = {"cancelled": False}

        async def waits_forever():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        stop = asyncio.ensure_future(shutdown.wait())
        asyncio.get_running_loop().call_later(0.02, shutdown.set)
        result = await asyncio.wait_for(main_module.start_or_stop(waits_forever(), stop), timeout=2)
        self.assertFalse(result)
        self.assertTrue(state["cancelled"])


if __name__ == "__main__":
    unittest.main()
