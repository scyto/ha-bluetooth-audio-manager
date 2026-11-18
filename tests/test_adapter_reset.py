"""Adapter reset — the remote replacement for unplugging the Bluetooth stick.

A Realtek USB stick carrying three A2DP streams got stuck twice (firmware
crash; stale connection handles and "link tx timeout") and only a replug
helped.  ``POST /api/adapter/reset`` offers two levels:

* ``soft`` — BlueZ powers the adapter off and on; speakers are then
  reconnected one by one instead of all at once.
* ``usb`` — USBDEVFS_RESET on the stick's device node, then an app restart.

These tests cover the decisions around the reset (finding the USB device,
call order, the guard against reconnect storms, rate limiting, the restart
fallback) and the HTTP contract; the ioctl itself is mocked.
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from bt_audio_manager.bluez import usb_reset
from bt_audio_manager.bluez.usb_reset import (
    USBDEVFS_RESET,
    UsbResetError,
    check_usb_device_access,
    reset_usb_device,
    usb_device_for_hci,
    usb_device_node,
)
from bt_audio_manager.manager import AdapterResetBusy, BluetoothAudioManager
from bt_audio_manager.reconnect import ReconnectService
from bt_audio_manager.web.api import create_api_routes


def _fake_sysfs(root, *, hci="hci1", busnum="1", devnum="5", usb=True):
    """``/sys/class/bluetooth/<hci>/device`` → USB interface below its device."""
    if usb:
        dev = os.path.join(root, "devices", "pci0000:00", "usb1", "1-8", "1-8.4")
        iface = os.path.join(dev, "1-8.4:1.0")
        os.makedirs(iface)
        with open(os.path.join(dev, "busnum"), "w") as f:
            f.write(busnum + "\n")
        with open(os.path.join(dev, "devnum"), "w") as f:
            f.write(devnum + "\n")
        target = iface
    else:
        target = os.path.join(root, "devices", "platform", "serial0")
        os.makedirs(target)
    cls = os.path.join(root, "class", "bluetooth", hci)
    os.makedirs(cls)
    os.symlink(target, os.path.join(cls, "device"))
    return target


class UsbDeviceLookupTest(unittest.TestCase):

    def test_finds_bus_and_device_number_above_the_interface(self):
        with tempfile.TemporaryDirectory() as root:
            _fake_sysfs(root, busnum="1", devnum="12")
            busnum, devnum, path = usb_device_for_hci("hci1", sysfs=root)
        self.assertEqual((busnum, devnum), (1, 12))
        self.assertTrue(path.endswith("1-8.4"))

    def test_unknown_hci(self):
        with tempfile.TemporaryDirectory() as root:
            _fake_sysfs(root)
            with self.assertRaises(UsbResetError):
                usb_device_for_hci("hci7", sysfs=root)

    def test_non_usb_adapter(self):
        with tempfile.TemporaryDirectory() as root:
            _fake_sysfs(root, usb=False)
            with self.assertRaises(UsbResetError):
                usb_device_for_hci("hci1", sysfs=root)

    def test_device_node_path(self):
        self.assertEqual(usb_device_node(1, 5), "/dev/bus/usb/001/005")
        self.assertEqual(usb_device_node(3, 127, devfs="/x"), "/x/003/127")


class UsbResetIoctlTest(unittest.TestCase):

    def test_reset_issues_usbdevfs_reset_on_the_node(self):
        with tempfile.TemporaryDirectory() as devfs:
            os.makedirs(os.path.join(devfs, "001"))
            open(os.path.join(devfs, "001", "005"), "w").close()
            with patch.object(usb_reset.fcntl, "ioctl") as ioctl:
                node = reset_usb_device(1, 5, devfs=devfs)
        self.assertEqual(node, os.path.join(devfs, "001", "005"))
        ioctl.assert_called_once()
        self.assertEqual(ioctl.call_args.args[1], USBDEVFS_RESET)
        self.assertEqual(USBDEVFS_RESET, 0x5514)

    def test_access_check_names_the_fix(self):
        with tempfile.TemporaryDirectory() as devfs:
            with self.assertRaises(UsbResetError) as ctx:
                check_usb_device_access(1, 5, devfs=devfs)
        self.assertIn("usb: true", str(ctx.exception))

    def test_access_check_passes_for_a_writable_node(self):
        with tempfile.TemporaryDirectory() as devfs:
            os.makedirs(os.path.join(devfs, "001"))
            open(os.path.join(devfs, "001", "005"), "w").close()
            node = check_usb_device_access(1, 5, devfs=devfs)
        self.assertTrue(node.endswith("001/005"))


def make_manager():
    m = BluetoothAudioManager.__new__(BluetoothAudioManager)
    m._adapter_reset_lock = asyncio.Lock()
    m._adapter_reset_at = None
    m._adapter_resetting = False
    m._usb_reset_task = None
    m._adapter_restart_task = None
    m._adapter_path = "/org/bluez/hci1"
    m.adapter = MagicMock()
    m.adapter.set_powered = AsyncMock()
    m.adapter.is_powered = AsyncMock(return_value=True)
    m.reconnect_service = MagicMock()
    m.reconnect_service.reconnect_all = AsyncMock()
    m.restart_requested = asyncio.Event()
    m._broadcast_status = MagicMock()
    m.ADAPTER_RESET_OFF_SECONDS = 0
    m.ADAPTER_USB_RESET_GRACE = 0
    return m


class SoftResetTest(unittest.IsolatedAsyncioTestCase):

    async def test_powers_off_then_on_then_reconnects_staggered(self):
        m = make_manager()
        order = []
        m.reconnect_service.cancel_all.side_effect = lambda: order.append("cancel")
        m.adapter.set_powered.side_effect = lambda on: order.append(f"powered={on}")
        m.reconnect_service.reconnect_all.side_effect = (
            lambda stagger: order.append(f"reconnect stagger={stagger}")
        )
        result = await m.reset_adapter("soft")
        self.assertEqual(result, {"mode": "soft", "powered": True, "adapters": ["/org/bluez/hci1"]})
        self.assertEqual(order, [
            "cancel", "powered=False", "powered=True",
            f"reconnect stagger={m.ADAPTER_RESET_RECONNECT_STAGGER}",
        ])
        self.assertFalse(m._adapter_resetting)

    async def test_guard_is_up_while_the_reconnect_is_scheduled(self):
        """Disconnect events during the reset must not start their own reconnects."""
        m = make_manager()
        seen = []
        m.adapter.set_powered.side_effect = lambda on: seen.append(m._adapter_resetting)
        m.reconnect_service.reconnect_all.side_effect = (
            lambda stagger: seen.append(m._adapter_resetting)
        )
        await m.reset_adapter("soft")
        self.assertEqual(seen, [True, True, True])
        self.assertFalse(m._adapter_resetting)

    async def test_adapter_not_coming_back_does_not_reconnect(self):
        m = make_manager()
        m.adapter.is_powered = AsyncMock(return_value=False)
        m.ADAPTER_RESET_POWER_TIMEOUT = 0
        result = await m.reset_adapter("soft")
        self.assertEqual(result, {"mode": "soft", "powered": False, "adapters": ["/org/bluez/hci1"]})
        m.reconnect_service.reconnect_all.assert_not_called()
        self.assertFalse(m._adapter_resetting)

    async def test_guard_cleared_when_bluez_refuses(self):
        m = make_manager()
        m.adapter.set_powered.side_effect = RuntimeError("org.bluez.Error.Failed")
        with self.assertRaises(RuntimeError):
            await m.reset_adapter("soft")
        self.assertFalse(m._adapter_resetting)

    async def test_second_reset_within_a_minute_is_refused(self):
        m = make_manager()
        await m.reset_adapter("soft")
        with self.assertRaises(AdapterResetBusy):
            await m.reset_adapter("soft")
        with self.assertRaises(AdapterResetBusy):
            await m.reset_adapter("usb")

    async def test_reset_while_one_is_running_is_refused(self):
        m = make_manager()
        await m._adapter_reset_lock.acquire()
        try:
            with self.assertRaises(AdapterResetBusy):
                await m.reset_adapter("soft")
        finally:
            m._adapter_reset_lock.release()

    async def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            await make_manager().reset_adapter("hard")


class UsbResetTest(unittest.IsolatedAsyncioTestCase):

    def _patches(self, *, access_error=None, reset_error=None):
        lookup = patch("bt_audio_manager.manager.usb_device_for_hci",
                       return_value=(1, 5, "/sys/devices/x/1-8.4"))
        access = patch("bt_audio_manager.manager.check_usb_device_access",
                       return_value="/dev/bus/usb/001/005",
                       side_effect=access_error)
        reset = patch("bt_audio_manager.manager.reset_usb_device",
                      return_value="/dev/bus/usb/001/005",
                      side_effect=reset_error)
        return lookup, access, reset

    async def test_resets_the_adapter_behind_our_hci_and_restarts(self):
        m = make_manager()
        m._request_self_restart = AsyncMock(return_value=True)
        lookup, access, reset = self._patches()
        with lookup as lk, access, reset as rs, patch(
            "bt_audio_manager.manager.asyncio.sleep", new=AsyncMock(),
        ):
            result = await m.reset_adapter("usb")
            self.assertEqual(result["mode"], "usb")
            self.assertEqual(result["device"], "/dev/bus/usb/001/005")
            self.assertTrue(m._adapter_resetting)
            m.reconnect_service.cancel_all.assert_called_once()
            await m._usb_reset_task
        lk.assert_called_once_with("hci1")
        rs.assert_called_once_with(1, 5)
        m._request_self_restart.assert_awaited_once()

    async def test_no_restart_of_our_own_when_the_adapter_watch_already_restarts(self):
        m = make_manager()
        m._request_self_restart = AsyncMock(return_value=True)
        m._adapter_restart_task = asyncio.get_running_loop().create_future()
        lookup, access, reset = self._patches()
        with lookup, access, reset, patch(
            "bt_audio_manager.manager.asyncio.sleep", new=AsyncMock(),
        ):
            await m.reset_adapter("usb")
            await m._usb_reset_task
        m._request_self_restart.assert_not_awaited()
        m._adapter_restart_task.cancel()

    async def test_falls_back_to_restart_event_without_supervisor(self):
        m = make_manager()
        m._request_self_restart = AsyncMock(return_value=False)
        lookup, access, reset = self._patches()
        with lookup, access, reset, patch(
            "bt_audio_manager.manager.asyncio.sleep", new=AsyncMock(),
        ):
            await m.reset_adapter("usb")
            await m._usb_reset_task
        self.assertTrue(m.restart_requested.is_set())

    async def test_missing_usb_access_fails_before_anything_happens(self):
        m = make_manager()
        lookup, access, reset = self._patches(access_error=UsbResetError("no write access"))
        with lookup, access, reset as rs:
            with self.assertRaises(UsbResetError):
                await m.reset_adapter("usb")
        rs.assert_not_called()
        m.reconnect_service.cancel_all.assert_not_called()
        self.assertIsNone(m._adapter_reset_at)  # can be retried at once
        self.assertFalse(m._adapter_resetting)

    async def test_failed_ioctl_reconnects_instead_of_restarting(self):
        m = make_manager()
        m._request_self_restart = AsyncMock(return_value=True)
        lookup, access, reset = self._patches(reset_error=OSError(5, "I/O error"))
        with lookup, access, reset, patch(
            "bt_audio_manager.manager.asyncio.sleep", new=AsyncMock(),
        ):
            await m.reset_adapter("usb")
            await m._usb_reset_task
        self.assertFalse(m._adapter_resetting)
        m.reconnect_service.reconnect_all.assert_awaited_once()
        m._request_self_restart.assert_not_awaited()


class ReconnectAllStaggerTest(unittest.IsolatedAsyncioTestCase):

    def _service(self, addresses, connected=()):
        mgr = MagicMock()
        mgr.config.auto_reconnect = True
        mgr.store.auto_connect_devices = [{"address": a} for a in addresses]
        mgr.managed_devices = {}
        for a in addresses:
            dev = MagicMock()
            dev.is_connected = AsyncMock(return_value=a in connected)
            mgr.managed_devices[a] = dev
        svc = ReconnectService(mgr)
        svc._running = True
        return svc

    async def test_connected_speakers_take_no_slot(self):
        """A restart with two of three still connected must not delay the third."""
        svc = self._service(["A", "B", "C"], connected=("A", "B"))
        started = []

        async def fake_loop(self_, address):
            started.append(address)

        with patch.object(ReconnectService, "_reconnect_loop", fake_loop), \
                patch("bt_audio_manager.reconnect.asyncio.sleep", new=AsyncMock()) as sl:
            await svc.reconnect_all(stagger=20)
            await asyncio.gather(*svc._tasks.values())
        self.assertEqual(started, ["C"])
        sl.assert_not_awaited()

    async def test_startup_staggers(self):
        """The app's startup reconnect is staggered (the stick got stuck twice
        right after all three speakers reconnected together)."""
        m = BluetoothAudioManager.__new__(BluetoothAudioManager)
        self.assertGreater(m.ADAPTER_RESET_RECONNECT_STAGGER, 0)
        import inspect
        src = inspect.getsource(BluetoothAudioManager.start)
        self.assertIn("reconnect_all(\n            stagger=self.ADAPTER_RESET_RECONNECT_STAGGER", src)

    async def test_stagger_starts_devices_one_after_another(self):
        svc = self._service(["A", "B", "C"])
        started = []

        async def fake_loop(self_, address):
            started.append((address, asyncio.get_running_loop().time()))

        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        with patch.object(ReconnectService, "_reconnect_loop", fake_loop), \
                patch("bt_audio_manager.reconnect.asyncio.sleep", fake_sleep):
            await svc.reconnect_all(stagger=20)
            await asyncio.gather(*svc._tasks.values())
        self.assertEqual(sorted(a for a, _ in started), ["A", "B", "C"])
        self.assertEqual(sorted(sleeps), [20, 40])

    async def test_without_stagger_nobody_waits(self):
        svc = self._service(["A", "B"])
        started = []

        async def fake_loop(self_, address):
            started.append(address)

        with patch.object(ReconnectService, "_reconnect_loop", fake_loop), \
                patch("bt_audio_manager.reconnect.asyncio.sleep", new=AsyncMock()) as sl:
            await svc.reconnect_all()
            await asyncio.gather(*svc._tasks.values())
        self.assertEqual(sorted(started), ["A", "B"])
        sl.assert_not_awaited()

    async def test_delayed_start_respects_auto_reconnect_off(self):
        svc = self._service(["A", "B"])
        started = []

        async def fake_loop(self_, address):
            started.append(address)

        async def turn_off_then_sleep(delay):
            svc._manager.config.auto_reconnect = False

        with patch.object(ReconnectService, "_reconnect_loop", fake_loop), \
                patch("bt_audio_manager.reconnect.asyncio.sleep", turn_off_then_sleep):
            await svc.reconnect_all(stagger=20)
            await asyncio.gather(*list(svc._tasks.values()))
        self.assertEqual(started, ["A"])

    async def test_skips_devices_already_reconnecting(self):
        svc = self._service(["A", "B"])
        running = asyncio.get_running_loop().create_future()
        svc._tasks["A"] = running
        started = []

        async def fake_loop(self_, address):
            started.append(address)

        with patch.object(ReconnectService, "_reconnect_loop", fake_loop):
            await svc.reconnect_all()
            await svc._tasks["B"]
        self.assertEqual(started, ["B"])
        self.assertIs(svc._tasks["A"], running)
        running.cancel()


class AdapterResetApiTest(AioHTTPTestCase):

    async def get_application(self):
        self.manager = MagicMock()
        self.manager.reset_adapter = AsyncMock(return_value={"mode": "soft", "powered": True})
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def test_default_mode_is_soft(self):
        resp = await self.client.post("/api/adapter/reset")
        self.assertEqual(resp.status, 200)
        self.manager.reset_adapter.assert_awaited_once_with("soft")

    async def test_usb_mode(self):
        self.manager.reset_adapter.return_value = {"mode": "usb", "restart_expected": True}
        resp = await self.client.post("/api/adapter/reset", json={"mode": "usb"})
        self.assertEqual(resp.status, 200)
        self.manager.reset_adapter.assert_awaited_once_with("usb")

    async def test_bad_mode(self):
        resp = await self.client.post("/api/adapter/reset", json={"mode": "hard"})
        self.assertEqual(resp.status, 400)
        self.manager.reset_adapter.assert_not_awaited()

    async def test_non_object_body(self):
        resp = await self.client.post("/api/adapter/reset", json=["usb"])
        self.assertEqual(resp.status, 400)

    async def test_invalid_json(self):
        resp = await self.client.post(
            "/api/adapter/reset", data="{", headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status, 400)

    async def test_busy_is_409(self):
        self.manager.reset_adapter.side_effect = AdapterResetBusy("too soon")
        resp = await self.client.post("/api/adapter/reset", json={"mode": "soft"})
        self.assertEqual(resp.status, 409)

    async def test_failure_is_500(self):
        self.manager.reset_adapter.side_effect = UsbResetError("hci1 is not a USB adapter")
        resp = await self.client.post("/api/adapter/reset", json={"mode": "usb"})
        self.assertEqual(resp.status, 500)
        self.assertIn("not a USB adapter", (await resp.json())["error"])


if __name__ == "__main__":
    unittest.main()
