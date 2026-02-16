# Bluetooth Audio Manager

Manage Bluetooth audio device connections (A2DP speakers and receivers) with
persistent pairing, automatic reconnection, and a web-based management UI.

## How it works

This app uses BlueZ (the Linux Bluetooth stack) via D-Bus to discover,
pair, and connect Bluetooth audio devices. Once connected, the device appears
as a PulseAudio sink that Home Assistant's audio system can use for TTS,
media playback, and automations.

**Key features:**

- Scan for nearby Bluetooth audio devices (A2DP)
- Pair and connect with one click from the web UI
- Auto-reconnect when devices disconnect or after reboots
- Optional keep-alive audio to prevent speaker auto-shutdown
- Per-device MPD instances for HA media_player integration and volume control
- Custom AppArmor security profile (principle of least privilege)

## Coexistence with Home Assistant Bluetooth

This app is designed to coexist safely with Home Assistant's built-in
Bluetooth integration (used for BLE sensors, beacons, etc.):

- Discovery uses `Transport=bredr` (Classic Bluetooth only), while HA scans
  BLE (Low Energy) — completely separate transports
- All Bluetooth operations go through BlueZ D-Bus (no raw HCI access)
- Discovery start/stop is reference-counted per client — our operations
  never affect HA's scanning
- The adapter's power, discoverable, and pairable states are never modified

## App Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `log_level` | `info` | Logging verbosity (debug, info, warning, error) |
| `audio device` | `default` | Do NOT touch this setting, it is not usedd |

## In App Settings
| Option | Default | Description |
|--------|---------|-------------|
| `reconnect_interval_seconds` | `30` | Initial reconnection delay |
| `reconnect_max_backoff_seconds` | `300` | Maximum reconnection delay |
| `scan_duration_seconds` | `15` | How long to scan for devices |


### Per-device keep-alive

Many Bluetooth speakers enter standby after a period of silence. Keep-alive
streams inaudible audio to prevent this. It is configured per-device in the
web UI: open the device's menu (three-dot icon) and select **Settings**.

Two methods are available:

- **infrasound** (recommended): Streams a 2 Hz sine wave at very low amplitude.
  Below human hearing threshold. Prevents speakers that detect digital silence
  from entering standby.
- **silence**: Streams PCM zeros. Lower CPU usage but some speakers still
  detect this as silence and shut down.

### Per-device MPD (Music Player Daemon)

Each connected speaker can optionally run its own MPD instance, exposing it as
a `media_player` entity in Home Assistant. This lets you use HA automations
(TTS, media playback, `media_player.volume_set`, etc.) to control the speaker.

Enable MPD per-device in the web UI: open the device's menu (three-dot icon),
select **Settings**, and toggle **Enable MPD**. A TCP port from the 6600–6609
pool is assigned automatically (or you can pick one manually).

**Hardware Volume** (1–100%, default 100%) controls the speaker's volume level
when MPD starts. MPD's software volume then acts as the single volume knob — so
`media_player.volume_set 0.5` in an automation means 50% perceived loudness.

| Scenario | Behavior |
| ---------- | ---------- |
| **MPD starts, no audio playing** | Speaker hardware set to configured %, MPD becomes the single volume knob |
| **MPD starts, audio already playing** | Hardware left alone, MPD synced to current hardware level |
| **Speaker button press** | Hardware volume change is synced to MPD → HA entity updates |
| **HA automation** `media_player.volume_set` | MPD software volume changes → effective output = that % |
| **TTS with volume preset** | Automation sets volume then speaks → plays at that level |

## General Usage

1. Open the app from the Home Assistant sidebar ("BT Audio")
2. Click **Scan for Devices** (make sure your speaker is in pairing mode)
3. Click **Pair** next to your device
4. Click **Connect** — the device will appear as a PulseAudio audio sink
5. Go to **Settings > System > Audio** to see/select the Bluetooth speaker
6. Use TTS, media player, or automations to play audio through it

## MPD Integration
1. Get the hostname of the app from the app page in HAOS app manager (e.g. d4261985-bluetooth-audio-manager)
2. In devices and integration click add integration
3. Find the MPD integration
4. fill in the fields:
   - hostname (use the one you got in step one)
   - password - leave blank
   - port - the port shown in the config for the player 


## Requirements

- A Bluetooth adapter (built-in or USB dongle) accessible to HAOS
- The Bluetooth adapter must be powered on (managed by HAOS, not this app)
- The target device must support A2DP (Advanced Audio Distribution Profile)

## Troubleshooting

**"Bluetooth adapter is not powered"**: Ensure your Bluetooth hardware is
recognized by HAOS. Check Settings > System > Hardware.

**Device not appearing in scan**: Make sure the speaker is in pairing mode.
Some devices exit pairing mode after 30-60 seconds.

**Connected but no audio**: Check Settings > System > Audio to verify the
Bluetooth sink is listed. Try setting it as the default output.

**Speaker keeps disconnecting**: Enable keep-alive for the device in the web
UI (device menu > Settings). Try the `infrasound` method if `silence` doesn't
work.

**`br-connection-key-missing` error when connecting**: The pairing keys stored
by BlueZ are out of sync with the speaker. Click **Forget** in the app UI,
then clear the pairing on the speaker itself (usually hold the Bluetooth button
for ~10 seconds until the speaker announces "ready to pair" or the LED enters
pairing mode). Then scan and pair again from the app.

**`Authentication Rejected` when pairing**: The speaker still has old pairing
keys for your system's Bluetooth address and is refusing the new pairing
attempt. Clear the speaker's paired-device list (hold the Bluetooth button for
~10 seconds) so both sides start fresh, then re-pair from the app.

**Existing BLE integrations stopped working**: This should not happen by
design. Check the app logs for errors and file an issue on GitHub.
