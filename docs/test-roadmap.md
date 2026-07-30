# Test Roadmap

Status: **Planned** — backlog of tests to add on top of the suite introduced in PR #290. Each item is independently actionable; work top-down.

Non-test backlog items live in [roadmap.md](roadmap.md).

## Constraints

CI has no Bluetooth adapter, no D-Bus, and no PulseAudio server. Everything here is exercisable with plain data. Anything needing a live `org.bluez` object, a real PA connection, or an actual speaker stays a manual hardware check.

Run the suite with:

```bash
PYTHONPATH=src python -m unittest discover -s tests -t . -v
```

## The habit that matters most

When a hardware-specific bug is diagnosed, capture the affected device's properties from its log into a test, and assert the **decision** that went wrong — not the hardware behaviour. `tests/test_discovery_filter.py` is built this way from the scan output attached to #286, so a regression reads as *"the Soundcore stopped being recognised"* rather than an abstract assertion failure.

Every issue with a log attached is a potential test fixture. That is the cheapest source of high-value cases this project has.

---

## Tier 1 — guards bugs this repo has actually had

### 1. `get_audio_devices()` end-to-end with a fake ObjectManager

**Where:** new `tests/test_get_audio_devices.py`
**Depends on:** #288 (assert the post-fix behaviour, so land it there or after)

`tests/test_discovery_filter.py` covers the helpers, but not the filter itself — the code that broke in #286 and that #288 changes. It consumes a `GetManagedObjects()` dict and returns a device list, which is entirely fakeable.

Build a fake bus exposing `introspect()` / `get_proxy_object()` returning a captured payload from a real scan, then assert exactly which devices are surfaced and which are skipped.

Cases worth encoding, all present in the #286 log:

- Soundcore 3 — vendor-only UUID + Audio/Video CoD → surfaced via CoD fallback
- DS1190 — no UUIDs + audio CoD → surfaced via CoD fallback
- Nebula projector — A2DP Source + AVRCP + Audio/Video CoD → **skipped** (the regression #287 would have introduced)
- normal speaker — A2DP Sink → surfaced via UUID match
- `cod_fallback=False` → CoD-only devices are **not** surfaced (the ghost-device guard)
- `cod_matched` is set correctly on the returned dict (the UI badge depends on it)
- `_logged_cache` dedupes repeat scans without suppressing accepted devices

**Why it's first:** "which devices show up" is the single most common support issue in this repo.

### 2. PulseAudio card profile name matching

**Where:** new `tests/test_pulse_profiles.py`
**Needs a small refactor first**

HAOS's native HFP backend names the profile `handsfree_head_unit`; oFono names it `headset_head_unit`; hyphenated variants exist too. The matching lives inside `activate_bt_card_profile()` in `src/bt_audio_manager/audio/pulse.py`, tangled with live PA calls.

Extract the decision — *given this list of profile names, pick the one for a2dp / hfp* — into a pure function, then test it against real profile lists captured from both backends, plus the no-match case.

**Why:** string matching against names that vary by backend, with no test, is exactly how silent breakage happens after a HAOS bump.

### 3. Config and settings validation

**Where:** new `tests/test_config.py`

`src/bt_audio_manager/config.py`: `AppConfig.load()`, `runtime_settings`, `save_settings`, `bt_adapter_is_mac`, `bt_adapter_is_legacy_hci`.

Cover: defaults when the settings file is absent, malformed JSON, unknown keys, out-of-range values, MAC vs `hciN` adapter forms, and round-tripping a save/load.

**Why:** users hand-edit these files; bad input should not take down startup.

---

## Tier 2 — cheap, catches renames

### 4. Web API contract

**Where:** new `tests/test_api.py`, using `aiohttp.test_utils.AioHTTPTestCase` with a fake manager

`src/bt_audio_manager/web/api.py` is 775 lines with no coverage. Most of the value is as a **contract test against the frontend**: assert that device dicts carry the keys `web/static/app.js` reads — `cod_matched`, `signal_quality`, `has_transport`, `bearers`, `rssi`, `uuids`.

Also worth: 404 on unknown device, 400 on malformed body, and that error responses carry a message the UI can display.

**Why:** a backend rename that silently blanks part of the UI is a real failure mode and invisible to every other test.

### 5. EventBus fan-out

**Where:** new `tests/test_events.py`

`src/bt_audio_manager/web/events.py` (40 lines): subscribe, emit reaches every subscriber, unsubscribe, no subscriber leak when a client disconnects mid-emit.

**Why:** small, and the WebSocket path is load-bearing for the whole UI.

### 6. Reconnect backoff schedule

**Where:** new `tests/test_reconnect.py`
**May need a small refactor**

`src/bt_audio_manager/reconnect.py`. If the delay schedule is a pure function of attempt count, assert the curve and the ceiling directly. If it is interleaved with `asyncio.sleep`, extract the schedule first.

### 7. `MPDManager._daemon_env()`

**Where:** extend `tests/test_mpd_config.py`
**Depends on:** #289

Assert `PULSE_PROP_module-stream-restore.id` is set to the per-speaker value, and that the rest of the inherited environment survives (`PATH`, `PULSE_SERVER`).

---

## Tier 3 — non-obvious, higher leverage than they look

### 8. AppArmor profile lint

**Where:** new `tests/test_apparmor_profile.py`

Parse `bluetooth_audio_manager/apparmor.txt` and cross-check it against what the code actually does:

- every path the code `flock()`s carries the `k` permission (`/data/**`, `/tmp/**`, `/root/.config/pulse/**`)
- paths the app reads/writes at runtime appear with matching permissions
- the dev slug's profile stays in sync with the stable one

**Why:** HAOS does **not** log AppArmor DENIED to dmesg or `ha host logs`, so these failures cannot be diagnosed from logs — they have to be caught by reading the profile. A test that does that reading automatically is worth well beyond its size. Pure text parsing, no hardware.

### 9. Add-on metadata consistency

**Where:** new `tests/test_addon_metadata.py`

Compare `bluetooth_audio_manager/config.yaml` and `bluetooth_audio_manager_dev/config.yaml`: identical options schema, identical `map:` entries, differing only in slug, name, image, and version.

**Why:** the Supervisor reads add-on metadata from `main`, and the dev build workflow syncs `config.yaml` and `apparmor.txt` from dev→main. Any *new* metadata file added to the dev slug must be added to that sync step too — a drift this test would surface. See #215.

### 10. Frontend helpers

**Where:** new `tests/js/`, run with `node --test`
**Only if the JS surface is worth a second toolchain**

`profileLabels()` and the device-card rendering helpers in `web/static/app.js` — mainly the UUID→label mapping.

---

## Explicitly out of scope

Mocking these tests the mock, not the system:

- live D-Bus wrappers — `bluez/device.py`, `bluez/media_player.py`, `bluez/agent.py`
- keep-alive subprocess plumbing — `audio/keepalive.py`
- real PulseAudio connections and sink state transitions
- anything whose correctness depends on BlueZ or PA behaviour rather than on our decisions
