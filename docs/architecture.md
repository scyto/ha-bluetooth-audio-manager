# Bluetooth Audio Manager — Architecture

This document describes the internal architecture of the Home Assistant Bluetooth Audio Manager add-on: how it works, why key design decisions were made, and how the components fit together.

---

## Table of Contents

- [Overview](#overview)
- [Project Layout](#project-layout)
- [Runtime Environment](#runtime-environment)
- [Application Startup](#application-startup)
- [Core Manager](#core-manager)
- [BlueZ / D-Bus Integration](#bluez--d-bus-integration)
- [Audio Subsystem](#audio-subsystem)
- [Device Persistence](#device-persistence)
- [Reconnection System](#reconnection-system)
- [Web UI & API](#web-ui--api)
- [Configuration System](#configuration-system)
- [CI/CD Pipeline](#cicd-pipeline)
- [Data Flow Examples](#data-flow-examples)
- [Design Decisions & Trade-offs](#design-decisions--trade-offs)
- [Known Limitations](#known-limitations)

---

## Overview

The Bluetooth Audio Manager is a Home Assistant add-on that manages Bluetooth audio devices (A2DP speakers and headphones). It provides:

- **Device discovery and pairing** via BlueZ over D-Bus
- **Audio routing** through PulseAudio (A2DP stereo or HFP mono)
- **Persistent device storage** with per-device settings
- **Auto-reconnection** with exponential backoff
- **Keep-alive streaming** to prevent speaker auto-shutdown
- **MPD integration** for per-device media player entities in HA
- **AVRCP media controls** so speaker buttons (play/pause/volume) work
- **Real-time web UI** with WebSocket event streaming

The application is written in Python 3.12, runs in an Alpine Linux Docker container, and communicates with the host's Bluetooth and audio stacks via mounted D-Bus and PulseAudio sockets.

---

## Project Layout

```
ha-bluetooth-audio-manager/
├── src/bt_audio_manager/                # Python application
│   ├── __main__.py                      # Entry point & async bootstrap
│   ├── config.py                        # Configuration loader
│   ├── manager.py                       # Core orchestrator (~1300 lines)
│   ├── reconnect.py                     # Auto-reconnection service
│   ├── bluez/                           # BlueZ D-Bus wrappers
│   │   ├── adapter.py                   # BT adapter discovery/control
│   │   ├── device.py                    # Device pair/connect/monitor
│   │   ├── agent.py                     # Pairing agent (Just Works)
│   │   ├── constants.py                 # UUIDs, D-Bus paths, flags
│   │   └── media_player.py             # MPRIS player for AVRCP buttons
│   ├── audio/                           # Audio management
│   │   ├── pulse.py                     # PulseAudio sink control
│   │   ├── mpd.py                       # Per-device MPD daemons
│   │   └── keepalive.py                 # Inaudible audio streaming
│   ├── persistence/
│   │   └── store.py                     # JSON device store
│   └── web/                             # Web UI & API
│       ├── server.py                    # aiohttp server + static files
│       ├── api.py                       # REST endpoints
│       ├── events.py                    # EventBus + WebSocket pub/sub
│       ├── log_handler.py              # Log streaming to UI
│       └── static/                      # Frontend (HTML/JS/CSS)
├── docker/                              # Container build
│   ├── Dockerfile                       # Alpine 3.20 image
│   ├── requirements.txt                 # Python dependencies
│   └── rootfs/etc/                      # s6-overlay init scripts
├── bluetooth_audio_manager/             # HA add-on manifest (stable)
├── bluetooth_audio_manager_dev/         # HA add-on manifest (dev channel)
├── .github/workflows/build.yaml         # CI/CD pipeline
└── docs/                                # Documentation
```

---

## Runtime Environment

The add-on runs in a Docker container managed by the HA Supervisor. It requires host-level access to two subsystems:

### D-Bus (BlueZ)

The container mounts the host's D-Bus system bus socket at `/run/dbus/system_bus_socket`. All Bluetooth operations — discovery, pairing, connecting, profile management — go through BlueZ's D-Bus API using the `dbus_next` library.

**Why D-Bus?** BlueZ is the standard Linux Bluetooth stack. It exposes its API exclusively over D-Bus. There is no alternative for managing Bluetooth on HAOS.

### PulseAudio

The container mounts the host's PulseAudio socket (typically `/run/audio/pulse.sock` or `/run/audio/native`). Audio sink management — profile switching, volume control, suspend/resume — uses `pulsectl_asyncio`.

**Why PulseAudio?** HAOS uses PulseAudio as its audio server. BlueZ hands off audio transport to PA once a device is connected.

### Required Permissions

```yaml
host_dbus: true                    # D-Bus system bus access
audio: true                        # PulseAudio socket mount
privileged: [NET_ADMIN, NET_RAW]   # Bluetooth HCI access
hassio_api: true                   # Supervisor API (for restart)
```

---

## Application Startup

Entry point: `src/bt_audio_manager/__main__.py`

The startup sequence is ordered to ensure the web UI is available as early as possible (so HA ingress doesn't show 502 errors):

```
1. Load configuration (options.json + settings.json)
2. Configure logging
3. Start web server on port 8099          ← UI available immediately
4. Start BluetoothAudioManager            ← Bluetooth/audio init
5. Register signal handlers (SIGTERM/SIGINT)
6. Wait for shutdown event
```

The manager's `start()` method then initializes subsystems in dependency order:

```
1. Connect to D-Bus system bus
2. Resolve configured Bluetooth adapter
3. Register pairing agent (NoInputNoOutput)
4. Register MPRIS media player (for AVRCP)
5. Load persisted devices from JSON store
6. Block HFP profile (force AVRCP volume control)
7. Connect to PulseAudio
8. Initialize BluezDevice wrappers for stored devices
9. Clean stale devices from BlueZ cache
10. Detect already-connected (unmanaged) devices
11. Start reconnection service
12. Auto-reconnect all stored devices with auto_connect=true
13. Start per-device features (MPD, idle handlers)
14. Begin sink state polling loop
```

**Graceful shutdown** reverses this order: stop polling, cancel reconnection, disconnect devices, close D-Bus and PA connections.

---

## Core Manager

**File:** `src/bt_audio_manager/manager.py`

The `BluetoothAudioManager` class is the central orchestrator. It coordinates all subsystems and maintains the authoritative device state.

### Key State

| Field | Type | Purpose |
|-------|------|---------|
| `managed_devices` | `dict[str, BluezDevice]` | Active device wrappers by address |
| `_connecting` | `set[str]` | Addresses with in-progress connections |
| `_suppress_reconnect` | `set[str]` | Addresses where user disconnected (skip auto-reconnect) |
| `_keepalives` | `dict[str, KeepAliveService]` | Per-device keep-alive streams |
| `_mpd_instances` | `dict[str, MPDManager]` | Per-device MPD daemons |
| `_pending_suspends` | `dict[str, Task]` | Delayed power-save suspend timers |
| `_auto_disconnect_tasks` | `dict[str, Task]` | Delayed auto-disconnect timers |
| `_device_lifecycle_locks` | `dict[str, Lock]` | Per-device serialization locks |

### Device Connection Flow

```
connect_device(address)
  ├─ Cancel any pending auto-reconnect
  ├─ Acquire per-device lifecycle lock
  ├─ Get or create BluezDevice wrapper
  ├─ Call BlueZ Connect()
  ├─ Wait for D-Bus services (timeout 10s)
  ├─ Watch for AVRCP MediaPlayer signals
  ├─ Activate PA card profile (A2DP or HFP)
  │   └─ 3-step fallback: direct → ConnectProfile → module reload
  ├─ Wait for PA sink to appear (timeout 30s)
  ├─ Apply idle mode (keep-alive / power-save / auto-disconnect)
  ├─ Start MPD if enabled
  ├─ Record connection time
  └─ Broadcast devices_changed event
```

### Sink State Polling

Every 5 seconds, the manager queries PulseAudio for all Bluetooth sinks and tracks state transitions:

- **idle → running**: Audio started playing. Start keep-alive if configured, cancel auto-disconnect timer.
- **running → idle**: Audio stopped. Start power-save delay timer or auto-disconnect timer based on idle mode.

Only broadcasts `devices_changed` events when the snapshot actually differs from the previous poll.

---

## BlueZ / D-Bus Integration

**Directory:** `src/bt_audio_manager/bluez/`

### Adapter (`adapter.py`)

Wraps a BlueZ adapter (e.g., `/org/bluez/hci0`).

**Coexistence-safe discovery:** Before starting discovery, the app sets a discovery filter:
```python
SetDiscoveryFilter(Transport="bredr", UUIDs=[A2DP_SINK, HFP, HSP])
```

This restricts scanning to classic Bluetooth (BR/EDR) audio devices only. BlueZ reference-counts discovery per client, so this filter does **not** interfere with HA's passive BLE scanning for Zigbee proxies, ESPHome, etc.

### Device (`device.py`)

Wraps a BlueZ device (e.g., `/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF`).

Key operations:
- `pair()` → Authenticate (agent handles authorization)
- `set_trusted(True)` → BlueZ-level auto-reconnect on boot
- `connect()` → Establish link + profiles
- `connect_profile(uuid)` → Explicitly activate a single profile
- `disconnect()` / `forget()` → Drop connection / unpair

Subscribes to D-Bus `PropertiesChanged` signals to detect connection state changes and AVRCP events.

### Pairing Agent (`agent.py`)

Implements `org.bluez.Agent1` with `NoInputNoOutput` capability (Just Works pairing). Auto-approves all authorization requests. This matches the typical use case — the user initiates pairing from the web UI, so no PIN entry is needed.

### AVRCP Media Player (`media_player.py`)

Implements the `org.mpris.MediaPlayer2.Player` D-Bus interface. BlueZ routes AVRCP button commands from the speaker to this player:

- Play, Pause, Stop, Next, Previous, Seek
- Volume changes (absolute volume via AVRCP 1.4+)

Each command updates internal state, emits a D-Bus `PropertiesChanged` signal (so the speaker's display stays in sync), and invokes a callback to the manager.

**Important:** All MPRIS properties are registered as read-only (`PropertyAccess.READ`). `dbus_next` defaults properties to read-write, which causes a crash ("writable but does not have a setter") for properties with no setter.

### Constants (`constants.py`)

Bluetooth UUIDs used for filtering and profile selection:

| UUID | Profile |
|------|---------|
| `0000110b` | A2DP Sink (audio receiver) |
| `0000110a` | A2DP Source (audio sender) |
| `0000110c` | AVRCP Target |
| `0000110e` | AVRCP Controller |
| `0000111e` | HFP (Hands-Free) |
| `00001108` | HSP (Headset) |

---

## Audio Subsystem

**Directory:** `src/bt_audio_manager/audio/`

### PulseAudio Manager (`pulse.py`)

Manages Bluetooth audio sinks in PulseAudio.

**Connection:** Probes known HAOS socket paths (`/run/audio/pulse.sock`, `/run/audio/native`), with `PULSE_SERVER` env var as override.

**Key operations:**

- **Profile activation:** Switch a PA card between A2DP and HFP profiles. Profile names vary by PA backend:
  - HAOS (native HFP): `handsfree_head_unit`
  - oFono backend: `headset_head_unit`
  - The app tries both names plus hyphenated variants.

- **Sink lifecycle:** Wait for a Bluetooth sink to appear (polling with timeout), suspend/resume sinks for power save, monitor volume changes.

- **Event monitoring:** A separate PA connection subscribes to sink events (volume changes, state transitions). Auto-reconnects with backoff if PA restarts.

**Sink naming convention:**
```
bluez_sink.AA_BB_CC_DD_EE_FF.a2dp_sink    (A2DP stereo)
bluez_sink.AA_BB_CC_DD_EE_FF.hfp_sink     (HFP mono)
```

### MPD Manager (`mpd.py`)

Runs one MPD (Music Player Daemon) instance per connected device, on ports 6600–6609.

**Why MPD?** HA has a built-in MPD integration that creates `media_player` entities. By running an MPD instance per Bluetooth speaker and pointing it at the speaker's PA sink, each speaker appears as an independent media player in HA. This enables automations, voice assistant playback, and media browser support.

**Lifecycle:**
1. Generate config file (PulseAudio sink as output)
2. Start MPD process
3. Connect `python-mpd2` client
4. Route AVRCP button commands to MPD (play/pause/next/previous)

**Data:**
```
/data/mpd/
├── music/                  # Symlinks to music sources
├── playlists/
└── instance_6600/          # Per-device state
    ├── database
    └── state               # Saved playback position
```

### Keep-Alive Service (`keepalive.py`)

**Problem:** Many Bluetooth speakers auto-shutdown after 30–120 seconds of silence to save battery.

**Solution:** Stream inaudible audio to keep the connection alive.

Two methods:
- **Infrasound** (default): 2 Hz sine wave at low amplitude. Below the ~20 Hz hearing threshold but fools most speakers' silence detection.
- **Silence**: PCM zeros. Minimal CPU but some speakers detect digital silence and still sleep.

**Implementation:** Every 5 seconds, spawns a short `pacat` subprocess that pipes 1 second of audio to the speaker's PA sink. This burst pattern minimizes CPU and power usage.

---

## Device Persistence

**File:** `src/bt_audio_manager/persistence/store.py`

Devices are stored in `/data/paired_devices.json`:

```json
{
  "devices": [
    {
      "address": "AA:BB:CC:DD:EE:FF",
      "name": "Living Room Speaker",
      "auto_connect": true,
      "paired_at": "2026-02-17T12:34:56.789123+00:00",
      "audio_profile": "a2dp",
      "idle_mode": "keep_alive",
      "keep_alive_method": "infrasound",
      "power_save_delay": 60,
      "auto_disconnect_minutes": 30,
      "mpd_enabled": true,
      "mpd_port": 6600,
      "mpd_hw_volume": 100,
      "avrcp_enabled": true
    }
  ]
}
```

**Atomic writes:** Data is written to a `.tmp` file first, then moved into place with `os.replace()`. This prevents corruption if the container is killed mid-write.

**No device count limit.** MPD ports (6600–6609) limit simultaneous MPD instances to 10, but devices themselves have no cap.

### Per-Device Settings

| Setting | Values | Purpose |
|---------|--------|---------|
| `audio_profile` | `a2dp`, `hfp` | Stereo vs mono+microphone |
| `idle_mode` | `default`, `keep_alive`, `power_save`, `auto_disconnect` | What to do when audio stops |
| `keep_alive_method` | `infrasound`, `silence` | How to keep speaker awake |
| `power_save_delay` | 0–300 seconds | Delay before suspending sink |
| `auto_disconnect_minutes` | 5–60 minutes | Idle time before disconnect |
| `mpd_enabled` | boolean | Run MPD for this device |
| `mpd_port` | 6600–6609 | MPD listen port |
| `avrcp_enabled` | boolean | Track media button events |

---

## Reconnection System

**File:** `src/bt_audio_manager/reconnect.py`

When a device unexpectedly disconnects, the `ReconnectService` attempts to restore the connection using exponential backoff:

```
Attempt 1:  10 seconds  (quick retry — often a transient dropout)
Attempt 2:  30 seconds
Attempt 3:  45 seconds
Attempt 4:  ~67 seconds
...
Attempt N:  300 seconds (capped)
```

Backoff multiplier is 1.5× with jitter to prevent thundering herd.

**Conditions for auto-reconnect:**
- Global `auto_reconnect` setting is enabled
- Per-device `auto_connect` flag is true
- Device exists in the persistent store
- Disconnect was **not** user-initiated (user disconnects set `_suppress_reconnect`)

On app startup, `reconnect_all()` attempts to reconnect every stored device that has `auto_connect=true`.

---

## Web UI & API

### Web Server (`web/server.py`)

An `aiohttp` application running on port 8099, exposed through HA's ingress proxy.

**Static assets are served from `/res/`** instead of the conventional `/static/`. This is a deliberate workaround: HA's frontend service worker matches any URL containing `/static/` and applies `CacheFirst` caching with `ignoreSearch: true`. This means query-string cache busting (`?v=123`) doesn't work, and users would see stale assets. Serving from `/res/` bypasses the service worker entirely.

### REST API (`web/api.py`)

All endpoints are under `/api/`:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/api/health` | Health check (used by Supervisor watchdog) |
| GET | `/api/info` | Version, adapter info |
| GET | `/api/adapters` | List Bluetooth adapters |
| POST | `/api/set-adapter` | Switch active adapter |
| GET | `/api/devices` | List all devices with status |
| POST | `/api/scan` | Start Bluetooth discovery |
| GET | `/api/scan/status` | Check if scan is running |
| POST | `/api/pair` | Pair a discovered device |
| POST | `/api/connect` | Connect a paired device |
| POST | `/api/disconnect` | Disconnect a device |
| POST | `/api/forget` | Unpair and remove a device |
| POST | `/api/force-reconnect` | Force disconnect + reconnect cycle |
| PUT | `/api/devices/{addr}/settings` | Update per-device settings |
| GET | `/api/settings` | Get app settings |
| PUT | `/api/settings` | Update app settings |
| POST | `/api/restart` | Restart add-on via Supervisor |

BlueZ D-Bus errors are mapped to user-friendly messages:
- "Page Timeout" → "Device not responding. Make sure it is in pairing mode and nearby."
- "In Progress" → "A pairing attempt is already in progress."
- "Already Exists" → "Device is already paired."

### WebSocket Events (`web/events.py`)

Real-time updates are delivered over WebSocket at `/api/ws`.

**Why WebSocket instead of SSE?** HA Core's ingress proxy enables deflate compression for `text/event-stream`, which breaks SSE streaming. WebSocket bypasses both the compression bug and the service worker. All major HA add-ons (ESPHome, Z2M, Node-RED) use WebSocket for the same reason.

**EventBus:** Simple pub/sub using `asyncio.Queue` (one queue per connected client, max size 64).

Events emitted:

| Event | Payload | Trigger |
|-------|---------|---------|
| `devices_changed` | Full device list | Any device state change |
| `scan_started` | Duration | Discovery begins |
| `scan_finished` | (empty or error) | Discovery ends |
| `avrcp_event` | Address, property, value | Speaker button/volume |
| `mpris_event` | Address, event type | Play/pause/stop commands |
| `status` | Message string | Progress updates |
| `log_entry` | Timestamp, level, message | Application log output |

**Ring buffers:** Recent MPRIS and AVRCP events (50 each) are replayed to newly connecting clients so the UI catches up immediately.

### Log Streaming (`web/log_handler.py`)

A custom `logging.Handler` captures all application log output and:
1. Maintains a ring buffer of 500 recent entries
2. Emits each entry as a `log_entry` WebSocket event
3. New WebSocket clients receive the buffer on connect

The UI provides level filtering, text search, and auto-scroll.

### Frontend (`web/static/`)

A single-page application built with vanilla JavaScript, Bootstrap 5, and Font Awesome.

**Three views:**
- **Devices** — Card grid showing each device with status, badges, and action buttons
- **Events** — Real-time MPRIS/AVRCP event log
- **Logs** — Live application log with filtering

**WebSocket reconnection:** On disconnect, shows a banner with elapsed time and attempts reconnection with randomized backoff (1–10 seconds).

---

## Configuration System

**File:** `src/bt_audio_manager/config.py`

Three-tier configuration with fallback:

| Priority | Source | File | Managed By |
|----------|--------|------|------------|
| 1 | Runtime settings | `/data/settings.json` | Web UI (no restart needed) |
| 2 | HA options | `/data/options.json` | HA config page (restart needed) |
| 3 | Defaults | In-memory | Hardcoded |

**HA options** only controls `log_level`. All other settings (adapter, reconnect params, scan duration) are managed through the web UI and persisted in `settings.json`.

### Adapter Selection

The `bt_adapter` setting supports three formats:
- `"auto"` — Pick the first powered adapter, or first available
- `"AA:BB:CC:DD:EE:FF"` — Select by MAC address (current format)
- `"hciN"` — Legacy format, auto-migrated to MAC on first read

---

## CI/CD Pipeline

**File:** `.github/workflows/build.yaml`

See [docs/ci-workflow.md](ci-workflow.md) for detailed CI documentation.

### Dual-Channel Release

**Stable** (tags on `main`):
1. Multi-arch Docker build (amd64, aarch64, armv7, armhf)
2. Push to `ghcr.io/scyto/ha-bluetooth-audio-manager:{version}`
3. Version from `bluetooth_audio_manager/config.yaml`

**Dev** (`dev` branch pushes):
1. Same multi-arch build
2. Version: `sha-{short_commit_hash}`
3. Push to `ghcr.io/scyto/ha-bluetooth-audio-manager-dev:sha-{hash}`
4. Auto-create PR on `main` to update `bluetooth_audio_manager_dev/config.yaml`
5. Direct-merge the version PR

---

## Data Flow Examples

### Speaker Button Press → UI Update

```
Speaker sends AVRCP "Play" via Bluetooth
  → BlueZ routes to registered MPRIS player
    → media_player.py: Play() called
      → Update internal PlaybackStatus
      → Emit D-Bus PropertiesChanged (speaker display syncs)
      → Invoke callback to manager
        → If MPD enabled: send MPD play command
        → EventBus emits avrcp_event
          → WebSocket delivers to all connected UIs
            → UI updates event log
```

### Unexpected Disconnect → Auto-Reconnect

```
Bluetooth link drops
  → BlueZ fires PropertiesChanged(Connected=false)
    → BluezDevice callback triggers
      → Manager: cancel keep-alive, MPD, idle timers
      → Broadcast devices_changed
      → ReconnectService: schedule reconnect
        → 10s: attempt 1 (quick retry)
        → 30s: attempt 2 (if quick retry failed)
        → 45s → 67s → 100s → ... → 300s (capped)
      → On success: restore idle mode, restart MPD
```

### Idle Mode: Keep-Alive

```
Device connected, audio stops (PA sink goes idle)
  → Sink polling detects running → idle transition
  → KeepAliveService starts
    → Every 5s: spawn pacat, stream 1s of 2 Hz infrasound
    → Speaker stays powered on
  → Audio resumes (sink goes running)
    → KeepAliveService stops (real audio takes over)
```

---

## Design Decisions & Trade-offs

### Why asyncio everywhere?

All I/O in this application — D-Bus, PulseAudio, HTTP, WebSocket — is inherently asynchronous. Using `asyncio` throughout avoids threads, simplifies concurrency, and enables per-device lifecycle locks that prevent race conditions during connect/disconnect sequences.

### Why per-device locks?

Bluetooth connection setup involves multiple sequential steps (BlueZ connect → wait for services → activate profile → wait for sink). Without locks, concurrent operations on the same device (e.g., user clicks "connect" while auto-reconnect is running) could interleave and leave the device in an inconsistent state.

### Why no BLE scanning?

The add-on only scans for classic Bluetooth (BR/EDR) audio devices. BLE (Low Energy) is used by HA Core for device tracking, ESPHome proxies, and Zigbee. By setting a `bredr` transport filter, the add-on's discovery doesn't interfere with any LE operations. BlueZ reference-counts discovery per client, so both can coexist.

### Why vanilla JavaScript?

The UI is simple enough that a framework would add complexity without benefit. Bootstrap handles layout/components, and the WebSocket event model maps naturally to DOM updates. No build step means assets are served directly.

### Why `/res/` instead of `/static/`?

HA's frontend service worker intercepts any URL containing `/static/` with `CacheFirst` strategy and `ignoreSearch: true`. This means:
1. Once cached, the browser never requests the file again
2. Query-string cache busting (`?v=hash`) is ignored by the service worker

Serving from `/res/` avoids the service worker's URL pattern entirely.

### Why WebSocket instead of SSE?

HA Core's ingress proxy enables deflate compression for `text/event-stream` responses, which breaks SSE streaming (the client receives garbled compressed chunks). WebSocket frames are handled differently by the proxy and work correctly.

### Why block HFP by default?

HFP (Hands-Free Profile) provides mono audio + microphone, but HAOS's audio container doesn't support `AF_BLUETOOTH` (SCO sockets) needed for HFP audio routing. Blocking HFP also forces the speaker to use AVRCP for volume control, which provides better volume sync between the UI and the physical device.

### Why MPD per device?

HA's MPD integration creates a `media_player` entity per MPD instance. Running one MPD per Bluetooth speaker means each speaker appears as an independent media player in HA, enabling per-speaker automations, voice assistant routing, and the HA media browser.

---

## Known Limitations

### HFP Profile Switching

**Status:** Disabled (`HFP_SWITCHING_ENABLED = False`)

HAOS's audio container doesn't expose `AF_BLUETOOTH` SCO sockets, so HFP audio can't be routed through PulseAudio. The UI hides the audio profile selector and defaults to A2DP. HFP-only devices (rare) won't work until HAOS adds SCO support.

### MPD Instance Limit

Ports 6600–6609 allow a maximum of 10 simultaneous MPD instances. Devices themselves have no count limit, but only 10 can have MPD active at once.

### PA Profile Name Variance

PulseAudio profile names for HFP differ between backends (native vs oFono). The app tries both known names plus hyphenated variants, but an unknown backend could require updates to the profile name list.

### AVRCP MediaPlayer Discovery Timing

After connecting, it can take several seconds for BlueZ to expose the device's AVRCP MediaPlayer interface. The app retries 3 times with 2-second delays and enforces a 60-second cooldown before searching again, but some speakers may not expose the interface at all.
