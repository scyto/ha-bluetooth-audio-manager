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
