# Bluetooth Audio Manager (Dev)

> **WARNING:** This is a development build tracking the `dev` branch and may
> be unstable. For the stable release, use **Bluetooth Audio Manager** instead.

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

- **Build info pill** — shows the version string (e.g. `sha-4f99686` for dev
  builds, or a semver like `0.2.5` for stable)
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
- Status badges: *Powered* / *Off*, *In Use*, *HA Bluetooth* (if managed by
  HA's Bluetooth integration), *HA BLE Scanning* (if active BLE scanning)

**Recommendation:** Use a dedicated USB Bluetooth adapter that is **not**
configured in Home Assistant's Bluetooth integration.

**Switching adapters:** Click **Select** on a different adapter. A confirmation
dialog warns that all current device pairings will be cleared. The add-on
restarts with the new adapter. The adapter selection is stored by MAC address
so it survives reboots.

## Usage

1. Open the add-on from the Home Assistant sidebar (**BT Audio Dev**)
2. If you have multiple Bluetooth adapters, go to **Settings > Bluetooth
   Adapters** and select the one to use
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
