# Tests

Hardware-free unit tests for the pure logic in `src/bt_audio_manager/`.

```bash
pip install -r docker/requirements.txt
PYTHONPATH=src python -m unittest discover -s tests -t . -v
```

CI runs the same command on every push to `dev`/`main` and every PR into
them (`.github/workflows/test.yaml`).

`pulsectl` `dlopen()`s `libpulse.so.0` when imported, so anything importing
`bt_audio_manager.audio` or `bt_audio_manager.manager` needs it installed
(`apt install libpulse0`). Those modules raise `unittest.SkipTest` when it
is missing, so the suite still runs on a macOS or Windows dev machine — CI
installs libpulse and additionally asserts every module imports, so a skip
can't quietly hide a broken import.

## Scope

There is no Bluetooth adapter, no D-Bus and no PulseAudio server in CI, so
these tests deliberately cover only logic that can be exercised with plain
data:

- decisions made *about* BlueZ/PulseAudio data (device filtering, CoD
  decoding, sink-name parsing, RSSI classification)
- text we generate for other programs to consume (`mpd.conf`)
- state we own outright (the JSON device store, MPD port allocation)

Anything that needs a live `org.bluez` object, a real PA connection, or an
actual speaker is out of scope and is verified by hand on hardware. When a
bug turns out to be hardware-specific, the useful thing to add here is a
test for the *decision* that went wrong, using the properties captured from
the affected device's logs — see `test_discovery_filter.py`, which is built
from real scan output attached to issue #286.

## Frontend escaping (`js/`)

Device names and adapter aliases come from Bluetooth advertisements, so
anything in radio range chooses them — and `app.js` renders them through
`innerHTML`. The escaping there is a security control, not formatting.

`js/test_escaping.js` runs on bare `node` with no toolchain or dependencies
and is part of the CI job:

```bash
node tests/js/test_escaping.js
```

It extracts the escaping helpers and `buildDeviceCard()` out of `app.js` by
name, so renaming them fails the suite loudly rather than silently skipping.

## Browser end-to-end (`e2e/`)

`e2e/test_xss_browser.py` is the ground truth for the above: it serves the
real UI from the real `WebServer` (with a mocked manager), drives Chromium
at it, feeds hostile device names through `renderDevices()` — the same entry
point the WebSocket handler uses — and asks the browser whether anything
executed.

It is **not** in the CI job, because it needs Playwright plus a ~95 MB
browser download. It self-skips when those are absent, so `discover` still
works everywhere. Run it when touching escaping or device rendering:

```bash
pip install playwright && playwright install chromium
PYTHONPATH=src python -m unittest tests.e2e.test_xss_browser -v
```
