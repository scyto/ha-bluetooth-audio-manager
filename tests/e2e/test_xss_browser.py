"""Browser-level XSS regression test for the device grid.

The unit-level equivalent is tests/js/test_escaping.js, which reasons about
the generated markup. This one is the ground truth: it serves the real UI
from the real aiohttp WebServer, loads it in Chromium, feeds hostile device
names through renderDevices() — the same entry point the WebSocket handler
uses — and asks the browser whether anything executed.

Device names arrive in Bluetooth advertisements, so they are attacker-chosen
for anyone in radio range.

Not part of the default CI job: it needs Playwright plus a ~95 MB browser
download. Run it locally when touching escaping or device rendering:

    pip install playwright && playwright install chromium
    PYTHONPATH=src python -m unittest tests.e2e.test_xss_browser -v

It self-skips when Playwright or the browser binary is unavailable.
"""

import asyncio
import socket
import unittest
from unittest.mock import MagicMock

try:
    from aiohttp import web
    from playwright.async_api import async_playwright
    _DEPS = True
except ImportError:  # pragma: no cover - environment without playwright
    _DEPS = False


# Each payload targets a different context in the card template. The tripwire
# sets window.__xss, so "did this execute" is answered by the browser rather
# than by pattern-matching markup.
PAYLOADS = [
    ("tag_injection_via_title", 'x"><img src=q onerror="window.__xss=1">'),
    ("handler_on_title_attr", 'x" onmouseover="window.__xss=1'),
    ("img_in_text_position", '<img src=q onerror="window.__xss=1">'),
    ("svg_onload", 'x"><svg onload="window.__xss=1">'),
    ("onclick_js_breakout", "'); window.__xss=1; ('"),
]

BASE_DEVICE = {
    "address": "AA:BB:CC:DD:EE:FF",
    "connected": True,
    "paired": True,
    "stored": True,
    "uuids": ["0000110b-0000-1000-8000-00805f9b34fb"],
    "rssi": -55,
    "signal_quality": "good",
    "adapter": "hci0",
    "mpd_enabled": True,
    "mpd_port": 6600,          # int, per openapi.yaml — exercises safeJsString coercion
    "mpd_hw_volume": 100,
    "audio_profile": "a2dp",
    "idle_mode": "default",
    "keep_alive_method": "infrasound",
    "power_save_delay": 0,
    "auto_disconnect_minutes": 30,
    "avrcp_enabled": True,
}

# Renders the device, lets auto-firing handlers run, then actively tries to
# trigger any handler that needs interaction before reporting.
PROBE_JS = """
(dev) => {
  window.__xss = 0;
  const grid = document.querySelector('#devices-grid');
  grid.innerHTML = '';
  let threw = null;
  try { renderDevices([dev]); } catch (e) { threw = e.message; }
  return new Promise((resolve) => setTimeout(() => {
    const title = grid.querySelector('.card-title');
    if (title) {
      for (const type of ['mouseover', 'focus', 'click', 'load', 'error']) {
        title.dispatchEvent(new MouseEvent(type, {bubbles: true}));
      }
    }
    setTimeout(() => resolve({
      xss: window.__xss,
      threw,
      injected: grid.querySelectorAll('img, svg, script, iframe, object').length,
      text: title ? title.textContent : null,
      cards: grid.querySelectorAll('.device-card').length,
    }), 50);
  }, 50));
}
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(_DEPS, "playwright/aiohttp not installed")
class DeviceGridXssTest(unittest.TestCase):
    """Hostile device names must never execute, and must still render."""

    def test_payloads_do_not_execute(self):
        results = asyncio.run(self._run())
        for name, payload, r in results:
            with self.subTest(payload=name):
                self.assertFalse(
                    r["xss"],
                    f"{name}: payload EXECUTED in the browser — {payload!r}",
                )
                self.assertEqual(
                    r["injected"], 0,
                    f"{name}: payload injected {r['injected']} element(s)",
                )
                # A card that fails to render is its own bug: buildDeviceCard
                # runs inside devices.map(), so a throw loses the whole grid.
                self.assertIsNone(r["threw"], f"{name}: render threw {r['threw']}")
                self.assertEqual(r["cards"], 1, f"{name}: card did not render")
                # Escaping must not mangle what the user sees.
                self.assertEqual(
                    r["text"], payload,
                    f"{name}: name not displayed verbatim as text",
                )

    async def _run(self):
        from bt_audio_manager.web.server import WebServer

        port = _free_port()
        server = WebServer(MagicMock())
        runner = web.AppRunner(server._app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()

        results = []
        try:
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.launch()
                except Exception as e:  # browser binary missing
                    raise unittest.SkipTest(f"chromium unavailable: {e}")
                page = await browser.new_page()
                # A real alert() would also prove execution; never let it block.
                page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

                await page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
                await page.wait_for_function(
                    "typeof renderDevices === 'function'", timeout=15000
                )

                for name, payload in PAYLOADS:
                    device = dict(BASE_DEVICE, name=payload)
                    results.append((name, payload, await page.evaluate(PROBE_JS, device)))

                await browser.close()
        finally:
            await runner.cleanup()
        return results


if __name__ == "__main__":
    unittest.main()
