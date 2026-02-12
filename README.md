# Bluetooth Audio Manager for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/scyto/ha-bluetooth-audio-manager?style=flat-square)](https://github.com/scyto/ha-bluetooth-audio-manager/releases)
[![Build Status](https://img.shields.io/github/actions/workflow/status/scyto/ha-bluetooth-audio-manager/build.yaml?branch=main&style=flat-square)](https://github.com/scyto/ha-bluetooth-audio-manager/actions/workflows/build.yaml)
[![GitHub Sponsors](https://img.shields.io/github/sponsors/scyto?style=flat-square)](https://github.com/sponsors/scyto)

A Home Assistant add-on that lets you manage Bluetooth audio devices (A2DP speakers and receivers) from a web UI — with persistent pairing, automatic reconnection, and volume control.

<!-- TODO: Add screenshot of the main dashboard -->

## Why This Exists

Home Assistant has no built-in way to manage Bluetooth audio devices. Without this add-on, connecting a Bluetooth speaker means SSH-ing into the host, manually running `bluetoothctl` to scan, pair, and connect, setting up PulseAudio sinks by hand, and hoping it all survives a reboot. It usually doesn't.

This add-on gives you a point-and-click UI right in the HA sidebar. Scan, pair, connect, done — and it reconnects automatically when things drop.

## Features

- **One-click device management** — scan, pair, connect, and disconnect from the web UI
- **Auto-reconnect** — reconnects after disconnects or reboots with configurable exponential backoff
- **AVRCP volume control** — hardware volume buttons on your speaker work correctly
- **Keep-alive streaming** — optional inaudible audio (infrasound or silence) prevents speakers from auto-shutting down during quiet periods
- **Multi-adapter support** — detects all Bluetooth adapters with friendly USB device names
- **Real-time monitoring** — live event log for AVRCP events and media commands, plus a filterable add-on log viewer
- **Safe BLE coexistence** — uses Classic Bluetooth (BR/EDR) only; HA's BLE integrations (sensors, beacons, ESPHome proxies) continue working without interference
- **Security-first** — custom AppArmor profile enforcing least-privilege access, all Bluetooth operations go through BlueZ D-Bus (no raw HCI)
- **Watchdog** — built-in health endpoint for automatic restart on failure

<!-- TODO: Add screenshot of the device cards / events view -->

## Supported Platforms

| Architecture | Examples |
| --- | --- |
| aarch64 | Raspberry Pi 4, Raspberry Pi 5 |
| amd64 | Intel NUCs, x86-64 VMs |
| armv7 | Raspberry Pi 3 |
| armhf | Older ARM devices |

**Requirements:**

- Home Assistant OS (or a setup with D-Bus, PulseAudio, and BlueZ)
- A Bluetooth adapter with BR/EDR (Classic Bluetooth) support — built-in or USB dongle
- The adapter must be powered on (managed by HAOS, not this add-on)
- Target devices must support A2DP (Advanced Audio Distribution Profile)

## Limitations

- **No audio receiving** — streams audio *to* speakers only. Cannot receive audio *from* Bluetooth devices (e.g., a phone streaming music to HA)
- **No LE Audio / LC3** — Classic A2DP only; Bluetooth Low Energy audio is not supported
- **No HFP/HSP** — Hands-Free Profile is intentionally blocked to ensure AVRCP volume control works reliably
- **Single active adapter** — one Bluetooth adapter active at a time (switchable in settings, requires add-on restart)
- **No multiroom sync** — each speaker is an independent PulseAudio sink; synchronized grouped playback is outside the scope of this add-on

## Installation

1. In Home Assistant, go to **Settings > Add-ons > Add-on Store**
2. Click the three-dot menu (top right) and select **Repositories**
3. Add the repository URL:

   ```text
   https://github.com/scyto/ha-bluetooth-audio-manager
   ```

4. Find **Bluetooth Audio Manager** in the store and click **Install**
5. Start the add-on — it appears in the sidebar as **BT Audio**

## Quick Start

1. Open **BT Audio** from the sidebar
2. Put your speaker in pairing mode
3. Click **Scan for Devices**
4. Click **Pair** next to your device
5. Click **Connect** — the speaker appears as a PulseAudio audio sink
6. Go to **Settings > System > Audio** to select it as the default output
7. Use TTS, media player, or automations to play audio through it

## Configuration

Configuration options and per-device keep-alive settings are documented in the [add-on documentation](bluetooth_audio_manager/DOCS.md).

## Links

- [Add-on Documentation](bluetooth_audio_manager/DOCS.md)
- [Report a Device Issue](https://github.com/scyto/ha-bluetooth-audio-manager/issues/new?template=device-issue.yml)
- [All Issues](https://github.com/scyto/ha-bluetooth-audio-manager/issues)
- [Sponsor this project](https://github.com/sponsors/scyto)
