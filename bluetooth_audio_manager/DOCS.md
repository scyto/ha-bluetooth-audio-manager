# Bluetooth Audio Manager

Manage Bluetooth audio device connections (A2DP speakers and receivers) with
persistent pairing, automatic reconnection, and a web-based management UI.

## How it works

This add-on uses BlueZ (the Linux Bluetooth stack) via D-Bus to discover,
pair, and connect Bluetooth audio devices. Once connected, the device appears
as a PulseAudio sink that Home Assistant's audio system can use for TTS,
media playback, and automations.

**Key features:**

- Discover nearby Bluetooth audio devices via the **Add Device** tile
- Pair and connect with one click from the web UI
- Auto-reconnect when devices disconnect or after reboots (exponential backoff)
- Four per-device idle modes: Default, Power Save, Stay Awake, Auto-Disconnect
- Per-device MPD instances for HA `media_player` integration and volume control
- Per-device AVRCP / Media Buttons toggle
- Bluetooth adapter selection (multiple USB dongles)
- Real-time Events view (MPRIS commands, AVRCP events, Transport volume)
- Live Logs view with level filtering and regex search
- WebSocket-based real-time updates with connection status indicator
- Dark mode (automatic system theme detection)
- Custom AppArmor security profile (principle of least privilege)

## Coexistence with Home Assistant Bluetooth

This add-on is designed to coexist safely with Home Assistant's built-in
Bluetooth integration (used for BLE sensors, beacons, etc.):

- Discovery uses `Transport=bredr` (Classic Bluetooth only), while HA scans
  BLE (Low Energy) — completely separate transports
- All Bluetooth operations go through BlueZ D-Bus (no raw HCI access)
- Discovery start/stop is reference-counted per client — our operations
  never affect HA's scanning
- The adapter's power, discoverable, and pairable states are never modified

## The web UI

### Header

The header bar contains:

- **Build info pill** — shows the version string (e.g. `2.1.0` for stable
  builds, or `sha-4f99686` for dev)
- **Views** dropdown — switch between **Events** and **Logs** views
- **Settings** dropdown — open **App Settings** or **Bluetooth Adapters**
- **Connection status** badge — shows the WebSocket state: *Connected*,
  *Connecting*, *Reconnecting*, or *Disconnected*
- **Refresh** button — manually refresh the device list

### Devices view (default)

A responsive grid of device cards plus the **Add Device** tile.

**Add Device tile** — click to start a Bluetooth discovery scan. While
scanning, the tile shows a spinner and countdown (e.g. "Scanning... 15s").
Discovered devices appear incrementally as the scan progresses.

**Device cards** show:

- **Device name** and **MAC address**
- **Status badge** — color-coded: green *Connected*, orange *Paired*,
  gray *Discovered*
- **Capability badges** (connected devices) — BR/EDR, A2DP, HFP, AVRCP
  with checkmarks (e.g. "A2DP ✓") for active profiles
- **Signal strength** (connected devices) — RSSI in dBm with a color-coded
  signal icon. Colors indicate live vs stale readings:
  - **Green** — excellent or good signal, actively updated
  - **Yellow** — fair signal, actively updated
  - **Red** — weak or very weak signal, actively updated (audio may stutter)
  - **Grey** — last known signal from discovery time. The device is BR/EDR-only
    (Classic Bluetooth) and cannot be measured while connected. Dual-mode
    devices (BLE + BR/EDR) refresh automatically every 60 seconds.
- **Audio sink info** (connected devices) — sample rate, channels, codec,
  volume percentage, and streaming state (Streaming / Idle / Suspended)
- **Feature badges** — enabled per-device features: Power Save, Stay Awake,
  Auto-Disconnect, MPD (with port number)
- **Action buttons** — contextual: *Connect* / *Disconnect* / *Pair* /
  *Dismiss*
- **Device menu** (**⋮**) — for paired/stored devices, contains:
  **Settings**, **Force Reconnect** (if connected), and **Forget Device**

### Events view

A real-time feed of media and volume events. Each entry shows a timestamp,
a color-coded type label, the event content, and the device name.

Event types:

- **MPRIS** — media player commands received from the speaker (Play, Pause,
  Next, Previous, etc.)
- **AVRCP** — property changes from speakers with AVRCP support (volume,
  playback status)
- **Transport** — A2DP transport volume events from speakers without AVRCP
  (e.g. initial volume on connect)

The view keeps the most recent 100 events and has a **Clear** button.

### Logs view

A live-streaming log viewer with:

- **Level filter** dropdown — All Levels, Debug, Info, Warning, Error
- **Search** input — filters by message text (supports regex)
- **Auto-scroll** toggle — keeps the view pinned to the latest entry
- **Live** toggle — pause or resume log streaming
- **Log count** badge

Each log entry shows a timestamp (with millisecond precision), level, logger
module name, and message.

## Configuration

### Add-on configuration (HA Settings page)

These options are set on the add-on's **Configuration** tab in Home Assistant
and require an add-on restart to take effect.

| Option | Default | Description |
| ------ | ------- | ----------- |
| `log_level` | `info` | Logging verbosity: `debug`, `info`, `warning`, `error` |
| `mpd_password` | *(empty)* | Optional password for all MPD instances. Leave empty for no auth. |

### App Settings (in-app)

Accessed via **Settings > App Settings** in the header. These take effect
immediately — no restart needed.

| Setting | Default | Range |
| ------- | ------- | ----- |
| Auto Reconnect | On | toggle |
| Reconnect Interval | 30 s | 5–600 s |
| Max Reconnect Backoff | 300 s | 60–3600 s |
| Scan Duration | 30 s | 5–120 s |

- **Auto Reconnect** — automatically reconnect to paired devices when they
  become available after a disconnect or reboot.
- **Reconnect Interval** — initial delay between reconnection attempts.
  Doubles on each failure up to the max backoff.
- **Max Reconnect Backoff** — ceiling for the exponential backoff.
- **Scan Duration** — how long the Add Device scan runs before stopping.

## Device settings

Open a device's menu (**⋮**) and select **Settings** to configure per-device
options. Settings are stored in `/config/paired_devices.json` (addon_config) and
persist across restarts, HA backups, and add-on reinstalls.

### Name

Your own name for the speaker, shown instead of its Bluetooth name (which
stays visible in small print on the card and in the settings dialog). Leave it
empty to use the Bluetooth name. The name is kept when the device is forgotten
and paired again.

### When Idle

Controls what happens when audio playback stops on a connected device.

| Mode | Description |
| ---- | ----------- |
| **Default** (do nothing) | No action taken. The speaker's own hardware idle timer determines when it sleeps. |
| **Power Save** (let speaker sleep) | Suspends the A2DP audio transport after a configurable delay: *Immediately*, *30 s*, *1 min*, or *5 min*. This releases the audio stream so the speaker's internal timer can power it down. |
| **Stay Awake** | Streams inaudible audio to prevent the speaker from auto-shutting down. Two methods: **Infrasound** (2 Hz sine wave — recommended; fools silence detection) or **Silence** (PCM zeros — lower CPU, but some speakers still detect this as silence). |
| **Auto-Disconnect** | Fully disconnects the Bluetooth device after a configurable idle timeout: *5*, *15*, *30*, or *60 minutes*. The device will reconnect automatically if Auto Reconnect is enabled. |

### MPD Media Player

Each connected speaker can optionally run its own MPD (Music Player Daemon)
instance, exposing it as a `media_player` entity in Home Assistant. This lets
you use HA automations (TTS, media playback, `media_player.volume_set`, etc.)
to control the speaker.

Enable MPD in the device's settings modal and toggle **MPD Media Player**.
A TCP port from the 6600–6609 pool is assigned automatically, or you can pick
one manually.

**Hardware Volume** (1–100%, default 100%) controls the speaker's volume level
when MPD starts. MPD's software volume then acts as the single volume knob — so
`media_player.volume_set 0.5` in an automation means 50% perceived loudness.

| Scenario | Behavior |
| -------- | -------- |
| **MPD starts, no audio playing** | Speaker hardware set to configured %, MPD becomes the single volume knob |
| **MPD starts, audio already playing** | Hardware left alone, MPD synced to current hardware level |
| **Speaker button press** | Hardware volume change is synced to MPD → HA entity updates |
| **HA automation** `media_player.volume_set` | MPD software volume changes → effective output = that % |
| **TTS with volume preset** | Automation sets volume then speaks → plays at that level |

To add the speaker to HA: install the **MPD** integration and point it at
the port shown in the device settings (e.g. port 6600). The hostname and
password status are displayed in **Settings > App Settings** under
"MPD Connection Info". Use that hostname when configuring the MPD integration
in HA.

### Media Buttons (AVRCP)

Toggle to enable or disable AVRCP media button tracking per device.

- **Enabled** (default): Tracks PlaybackStatus and accepts media-button
  commands from the speaker (play, pause, next, previous, volume). Events
  appear in the Events view.
- **Disabled**: Always reports PlaybackStatus as "Stopped". Useful if AVRCP
  registration prevents the speaker from entering power-save mode.

This toggle is greyed out for devices that do not advertise AVRCP UUIDs.

## Bluetooth Adapters

Accessed via **Settings > Bluetooth Adapters** in the header.

Lists all Bluetooth adapters detected on the system. Each adapter shows:

- Friendly name (resolved from USB hardware model when available)
- HCI name and modalias
- MAC address
- Status badges: *Powered* / *Off*, *Main adapter*, *In use · N speakers*
  (an additional adapter, see below), *HA Bluetooth* (if managed by HA's
  Bluetooth integration), *HA BLE Scanning* (if active BLE scanning)

**Names:** the pencil button gives an adapter a name of your own (e.g.
"Stick 87.5"). It is shown in this list, in the speaker settings and on the
device cards ("on Stick 87.5" instead of "on hci2"), and kept by MAC — also
when the adapter is unplugged or the devices are cleared. API:
`PUT /api/adapters/<MAC>/name {"name": "Stick 87.5"}`; an empty name clears
it.

**Recommendation:** Use a dedicated USB Bluetooth adapter that is **not**
configured in Home Assistant's Bluetooth integration.

**Switching the main adapter:** Click **Make main** on a different adapter.
This **unpairs every speaker and deletes its settings**; switching back does
not restore them. The confirmation dialog lists the speakers that will be
unpaired, and the switch only runs after you tick the acknowledgement. The
add-on then restarts with the new adapter. The adapter selection is stored by
MAC address so it survives reboots. To use an extra adapter for some speakers
only, do **not** switch — set it in those speakers' settings (see below).
Through the API, `POST /api/set-adapter` with `clean: true` needs
`confirm_unpair` set to the number from `GET /api/set-adapter/impact`.

**Several adapters:** one app instance can drive speakers on
more than one adapter — useful when one adapter cannot carry all streams
(three A2DP streams on one Realtek RTL8761B stick overload it now and then,
and every overload lowers the SBC bitrate for a long time). The adapter
chosen above stays the *main adapter*; each speaker can be moved to another
one:

1. Plug in the adapter. If Home Assistant's Bluetooth integration offers it,
   set it to **Ignore** there (an adapter HA uses cannot be chosen).
2. Open the speaker's **Settings**, choose the adapter under *Bluetooth
   adapter*, put the speaker in pairing mode and press **Pair on this
   adapter**. The app searches for the speaker on that adapter (up to 40 s),
   pairs and connects it there, removes its pairing on the other adapter and
   stores the choice. While it searches, the speaker is blocked on its old
   adapter so it cannot reconnect there; if the move fails, that is undone. Via the API: `POST /api/pair
   {"address": "<speaker>", "adapter": "<adapter MAC>"}`.

Everything else follows: connects and reconnects use the speaker's adapter,
the adapter is watched like the main one (unplugged and back → the app
restarts), `POST /api/adapter/reset` resets all adapters or the one given as
`adapter`, and `POST /api/scan` takes an `adapter` too. If an additional
adapter is missing at startup the app does not wait for it — its speakers are
unavailable until it appears, then it is used without a restart. Changing the
setting alone does not move a speaker; it keeps its old pairing until it is
paired on the new adapter.

## Usage

1. Open the add-on from the Home Assistant sidebar (**BT Audio**)
2. If you have multiple Bluetooth adapters, go to **Settings > Bluetooth
   Adapters** and make the one to use the main adapter (**Make main**) —
   before you pair any speakers
3. Put your Bluetooth speaker in pairing mode
4. Click the **Add Device** tile — a scan runs for the configured duration,
   showing a countdown
5. Discovered devices appear as cards. Click **Pair** on your device
6. Once paired, click **Connect** — the device appears as a PulseAudio
   audio sink
7. (Optional) Open the device menu (**⋮**) > **Settings** to configure idle
   mode, MPD, or AVRCP
8. Go to **Settings > System > Audio** in HA to see/select the Bluetooth
   speaker as the default output
9. Use TTS, media player, or automations to play audio through it

## Automating with the REST API

Everything this add-on's UI does is a REST call, and Home Assistant can make
those calls itself — so you can connect a speaker from an automation, script,
schedule, or dashboard button. This is the usual way to keep **Auto Reconnect**
turned off (so the add-on stops competing with a phone for the speaker) while
still connecting on demand.

No port forwarding and no API token are needed; HA reaches the add-on over its
internal network.

**Settings > API Reference** opens an interactive Swagger UI built into the
add-on. It documents every endpoint, lets you try calls against your real
hardware, and generates a ready-to-paste `rest_command` block with your own
hostname and device address already filled in:

```yaml
rest_command:
  bt_audio_connect:
    url: "http://YOUR-ADDON-HOSTNAME:8099/api/connect"
    method: post
    content_type: "application/json"
    payload: '{"address": "AA:BB:CC:DD:EE:FF"}'
```

Note that **Try it out** in the API Reference is not a sandbox — it acts on
your real devices.

Full guide with automation examples and troubleshooting:
<https://github.com/scyto/ha-bluetooth-audio-manager/blob/main/docs/api.md>

## Connection and playback behaviour

- **Startup** — speakers that are not connected are reconnected one after
  another, 20 s apart; speakers that are still connected take no slot.
- **Configured adapter missing at startup** — the app waits for the adapter
  configured by MAC instead of binding to the first powered one (on a host
  with two adapters usually Home Assistant's own). The UI stays reachable;
  choosing another adapter there ends the wait.
- **PulseAudio watchdog** — the connection to PulseAudio is checked with
  every sink poll (5 s). If the audio service was not up yet or is
  restarted later, the app reconnects by itself (one attempt every 10 s)
  and sets up the speakers that are still connected. `pactl` calls are
  limited to 10 s.
- **48 kHz streams** — with *Prefer 44.1 kHz* (App Settings) on, a speaker that set up its
  stream at 48 kHz by itself is disconnected and connected again from the
  app a few seconds later (at most two attempts per speaker in 10 minutes);
  re-opening only the stream could leave a speaker decoding at 48 kHz,
  about 9 % too fast.
- **Unreachable speaker, others on the same adapter** — every failed page
  ("page timeout") keeps the adapter's radio busy for about 5 s, and a
  speaker streaming on the same adapter overloads and loses bitrate. After
  a page timeout the speaker is tried at most every 120 s while another
  speaker on that adapter streams. `POST /api/connect` then answers 409 with
  `retry_in`; `POST /api/force-reconnect` is never held back.
- **Resume after a restart** — MPD cannot keep its own state file in the
  app container, so every new daemon starts with an empty queue. The app
  notes the station each MPD instance is playing (every 30 s and when it
  stops a daemon) in `/data/playback_resume.json` and plays it again when
  that speaker's daemon is back (entries older than 30 minutes are
  dropped). Stopped, forgotten or MPD-disabled speakers are not resumed.
- **Lowered SBC bitrate** — PulseAudio lowers a speaker's SBC bitpool after
  an overload and raises it again slowly. `GET /api/diagnostics/pulse-bluetooth`
  shows per speaker `sbc_frames_per_packet` (the value at full bitrate
  depends on the link's packet size, e.g. 5 or 7; 15 is the lowest step,
  flagged as `at_lowest_step`). A reconnect restores full bitrate. More than
  two streaming speakers per adapter tend to overload it; see *Several
  adapters* above.

## Requirements

- A Bluetooth adapter (built-in or USB dongle) accessible to HAOS
- The Bluetooth adapter must be powered on (managed by HAOS, not this add-on)
- The target device must support A2DP (Advanced Audio Distribution Profile)

## Troubleshooting

**"Bluetooth adapter is not powered"**: Ensure your Bluetooth hardware is
recognized by HAOS. Check Settings > System > Hardware.

**Device not appearing in scan**: Make sure the speaker is in pairing mode.
Some devices exit pairing mode after 30–60 seconds.

**Connected but no audio**: Check Settings > System > Audio to verify the
Bluetooth sink is listed. Try setting it as the default output.

**Speaker keeps disconnecting**: Open the device menu (**⋮**) > **Settings**
and configure the **When Idle** mode. Try **Stay Awake** with the *Infrasound*
method if *Silence* doesn't work.

**Zombie connection (connected but no audio or controls)**: Open the device
menu (**⋮**) and select **Force Reconnect**. This performs a full
disconnect/reconnect cycle to re-establish the audio link.

**Speaker won't enter power-save with AVRCP enabled**: Some speakers refuse to
sleep while an AVRCP media player is registered. Open the device menu (**⋮**) >
**Settings** and disable **Media Buttons (AVRCP)**, or set the idle mode to
**Power Save**.

**Device paired but "no audio profiles resolved" warning**: Some budget
Bluetooth speakers only advertise their audio capabilities via Class of Device
(CoD) and do not expose A2DP UUIDs until after pairing completes. Try
connecting the device — audio profiles typically resolve after the first
successful connection. If they don't, the device may not support A2DP.

**Multiple devices disconnected at once**: If several devices drop
simultaneously, the add-on detects this as a Bluetooth adapter disruption and
temporarily suppresses auto-reconnect to avoid hammering a potentially
unstable adapter. Reconnection resumes automatically after the suppression
window. Check the Logs view for "adapter disruption" messages.

**Adapter stuck — several speakers drop and won't come back**: A USB stick's
firmware can get into a state only a reset clears (kernel messages such as
`hw err`, `link tx timeout`, or packets for unknown connection handles).
`POST /api/adapter/reset` resets it without touching the stick:
`{"mode": "soft"}` powers the adapter off and on through BlueZ and reconnects
the speakers one by one; `{"mode": "usb"}` resets the USB device — like
unplugging it, firmware reload included — and restarts the add-on. Try `soft`
first. The add-on asks for USB access (`usb: true`) only for this reset.

**Devices reappeared after reinstall with default settings**: After a fresh
install or data wipe, the add-on automatically imports any devices that are
still paired in BlueZ. These devices are restored with default settings
(keep-alive off, MPD disabled). A toast notification confirms how many devices
were restored. Reconfigure per-device settings as needed.

**`br-connection-key-missing` error when connecting**: The pairing keys stored
by BlueZ are out of sync with the speaker. Open the device menu (**⋮**) and
select **Forget Device**, then clear the pairing on the speaker itself (usually
hold the Bluetooth button for ~10 seconds until the speaker announces "ready to
pair" or the LED enters pairing mode). Then scan and pair again.

**`Authentication Rejected` when pairing**: The speaker still has old pairing
keys for your system's Bluetooth address and is refusing the new pairing
attempt. Clear the speaker's paired-device list (hold the Bluetooth button for
~10 seconds) so both sides start fresh, then re-pair from the add-on.

**WebSocket disconnected / Reconnecting banner**: The UI shows a
"Reconnecting..." banner with elapsed time when the server connection is lost.
This is expected during add-on restarts or network interruptions. The
connection restores automatically with exponential backoff.

**Existing BLE integrations stopped working**: This should not happen by
design. Check the add-on logs for errors and file an issue on GitHub.
