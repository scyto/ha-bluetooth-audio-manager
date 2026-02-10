# Bluetooth Audio Manager

Manage Bluetooth audio device connections (A2DP speakers and receivers) with
persistent pairing, automatic reconnection, and a web-based management UI.

## How it works

This add-on uses BlueZ (the Linux Bluetooth stack) via D-Bus to discover,
pair, and connect Bluetooth audio devices. Once connected, the device appears
as a PulseAudio sink that Home Assistant's audio system can use for TTS,
media playback, and automations.

**Key features:**

- Scan for nearby Bluetooth audio devices (A2DP)
- Pair and connect with one click from the web UI
- Auto-reconnect when devices disconnect or after reboots
- Optional keep-alive audio to prevent speaker auto-shutdown
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

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `log_level` | `info` | Logging verbosity (debug, info, warning, error) |
| `auto_reconnect` | `true` | Automatically reconnect disconnected devices |
| `reconnect_interval_seconds` | `30` | Initial reconnection delay |
| `reconnect_max_backoff_seconds` | `300` | Maximum reconnection delay |
| `keep_alive_enabled` | `false` | Stream inaudible audio to prevent speaker sleep |
| `keep_alive_method` | `infrasound` | Keep-alive type: `silence` or `infrasound` |
| `scan_duration_seconds` | `15` | How long to scan for devices |

### Keep-alive methods

Many Bluetooth speakers enter standby after a period of silence:

- **infrasound** (recommended): Streams a 2 Hz sine wave at very low amplitude.
  Below human hearing threshold. Prevents speakers that detect digital silence
  from entering standby.
- **silence**: Streams PCM zeros. Lower CPU usage but some speakers still
  detect this as silence and shut down.

## Usage

1. Open the add-on from the Home Assistant sidebar ("BT Audio")
2. Click **Scan for Devices** (make sure your speaker is in pairing mode)
3. Click **Pair** next to your device
4. Click **Connect** — the device will appear as a PulseAudio audio sink
5. Go to **Settings > System > Audio** to see/select the Bluetooth speaker
6. Use TTS, media player, or automations to play audio through it

## Requirements

- A Bluetooth adapter (built-in or USB dongle) accessible to HAOS
- The Bluetooth adapter must be powered on (managed by HAOS, not this add-on)
- The target device must support A2DP (Advanced Audio Distribution Profile)

## Troubleshooting

**"Bluetooth adapter is not powered"**: Ensure your Bluetooth hardware is
recognized by HAOS. Check Settings > System > Hardware.

**Device not appearing in scan**: Make sure the speaker is in pairing mode.
Some devices exit pairing mode after 30-60 seconds.

**Connected but no audio**: Check Settings > System > Audio to verify the
Bluetooth sink is listed. Try setting it as the default output.

**Speaker keeps disconnecting**: Enable the keep-alive option in the add-on
configuration. Try the `infrasound` method if `silence` doesn't work.

**Existing BLE integrations stopped working**: This should not happen by
design. Check the add-on logs for errors and file an issue on GitHub.
