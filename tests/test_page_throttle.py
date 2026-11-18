"""A speaker that does not answer is paged less often while its adapter streams.

18 Nov 2026: 105.9 was unplugged and paged every 10-60 s ("page timeout",
about 5 s of radio time each); 97.0 on the same stick overloaded again and
again and PulseAudio dropped its bitrate to the bottom (hiss on FM).
"""

import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from bt_audio_manager.manager import BluetoothAudioManager
from bt_audio_manager.reconnect import ConnectDeferred, ReconnectService
from bt_audio_manager.web.api import create_api_routes

MAIN = "8A:88:4B:00:00:A1"
STICK2 = "8A:88:4B:00:00:B2"
B_1059 = "41:42:00:00:10:59"
B_970 = "41:42:00:00:09:70"
B_875 = "41:42:00:00:08:75"


def sink(addr):
    return f"bluez_sink.{addr.replace(':', '_')}.a2dp_sink"


def build(running=(B_970,)):
    m = BluetoothAudioManager.__new__(BluetoothAudioManager)
    m._adapter_address = MAIN
    m.store = MagicMock()
    m.store.get_device_settings.side_effect = lambda a: {"adapter": STICK2 if a == B_875 else ""}
    m._running_sinks = {sink(a) for a in running}
    m._poll_running_sinks = set()
    return m


class GateTest(unittest.TestCase):

    def test_no_timeout_no_wait(self):
        self.assertEqual(build().page_retry_wait(B_1059), 0.0)

    def test_timeout_with_streaming_neighbour_waits(self):
        m = build()
        m._note_connect_result(B_1059, RuntimeError("br-connection-page-timeout"))
        self.assertGreater(m.page_retry_wait(B_1059), 100)
        self.assertEqual(m._streaming_neighbours(B_1059), [B_970])

    def test_neighbour_on_other_adapter_does_not_count(self):
        m = build(running=(B_875,))
        m._note_connect_result(B_1059, RuntimeError("br-connection-page-timeout"))
        self.assertEqual(m.page_retry_wait(B_1059), 0.0)

    def test_own_sink_does_not_count(self):
        m = build(running=(B_1059,))
        m._note_connect_result(B_1059, RuntimeError("br-connection-page-timeout"))
        self.assertEqual(m.page_retry_wait(B_1059), 0.0)

    def test_spacing_expires_and_success_or_other_errors_clear(self):
        m = build()
        m._note_connect_result(B_1059, RuntimeError("br-connection-page-timeout"))
        m._last_page_timeout[B_1059] = time.monotonic() - m.PAGE_RETRY_SPACING - 1
        self.assertEqual(m.page_retry_wait(B_1059), 0.0)
        m._note_connect_result(B_1059, RuntimeError("br-connection-page-timeout"))
        m._note_connect_result(B_1059, None)
        self.assertEqual(m.page_retry_wait(B_1059), 0.0)
        m._note_connect_result(B_1059, RuntimeError("br-connection-busy"))
        self.assertEqual(m.page_retry_wait(B_1059), 0.0)


class ConnectTest(unittest.IsolatedAsyncioTestCase):

    async def test_connect_is_deferred_but_force_and_pair_are_not(self):
        m = build()
        m._note_connect_result(B_1059, RuntimeError("br-connection-page-timeout"))
        with self.assertRaises(ConnectDeferred):
            await m.connect_device(B_1059)
        with self.assertRaises(ConnectDeferred):
            await m.connect_device(B_1059, _from_reconnect=True)

        # Past the gate the real connect starts; stop it at the first step.
        m._connecting = set()
        m.reconnect_service = None
        m._clear_auto_reconnect_suppression = MagicMock()
        m._broadcast_status = MagicMock()
        m.event_bus = MagicMock()
        m._get_or_create_device = AsyncMock(side_effect=LookupError("reached connect"))
        for kwargs in ({"_force": True}, {"_from_pair": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(LookupError):
                await m.connect_device(B_1059, **kwargs)


class ReconnectLoopTest(unittest.IsolatedAsyncioTestCase):

    async def test_deferred_attempt_does_not_end_the_loop(self):
        manager = MagicMock()
        manager.config.auto_reconnect = True
        manager.config.reconnect_interval_seconds = 1
        manager.config.reconnect_max_backoff_seconds = 1
        manager.managed_devices = {}
        manager.connect_device = AsyncMock(side_effect=[
            ConnectDeferred("not now"), ConnectDeferred("not now"), True,
        ])
        svc = ReconnectService(manager)
        svc._running = True
        with patch("bt_audio_manager.reconnect.asyncio.sleep", AsyncMock()):
            await svc._reconnect_loop(B_1059)
        self.assertEqual(manager.connect_device.await_count, 3)


class ApiTest(AioHTTPTestCase):

    async def get_application(self):
        self.manager = MagicMock()
        self.manager.connect_device = AsyncMock(side_effect=ConnectDeferred("later"))
        self.manager.page_retry_wait.return_value = 87.4
        app = web.Application()
        app.router.add_routes(create_api_routes(self.manager))
        return app

    async def test_connect_returns_409_with_retry_time(self):
        resp = await self.client.post("/api/connect", json={"address": B_1059})
        self.assertEqual(resp.status, 409)
        data = await resp.json()
        self.assertTrue(data["deferred"])
        self.assertEqual(data["retry_in"], 87)


if __name__ == "__main__":
    unittest.main()
