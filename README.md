# Bluetooth Audio Manager for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/scyto/ha-bluetooth-audio-manager?style=flat-square)](https://github.com/scyto/ha-bluetooth-audio-manager/releases)
[![Build Status](https://img.shields.io/github/actions/workflow/status/scyto/ha-bluetooth-audio-manager/build.yaml?branch=main&style=flat-square)](https://github.com/scyto/ha-bluetooth-audio-manager/actions/workflows/build.yaml)
[![GitHub Sponsors](https://img.shields.io/github/sponsors/scyto?style=flat-square)](https://github.com/sponsors/scyto)

A Home Assistant add-on that lets you manage Bluetooth audio devices (A2DP speakers and receivers) from a web UI — with persistent pairing, automatic reconnection, and volume control.

<img width="1330" height="446" alt="image" src="https://github.com/user-attachments/assets/45cb568f-9d12-47bc-bcf0-6b34eb15f7c0" />

## Why This Exists

Home Assistant has no built-in way to manage Bluetooth audio devices. Without this add-on, connecting a Bluetooth speaker means SSH-ing into the host, manually running `bluetoothctl` to scan, pair, and connect, setting up PulseAudio sinks by hand, and hoping it all survives a reboot. It usually doesn't.

This add-on gives you a point-and-click UI right in the HA sidebar. Scan, pair, connect, done — and it reconnects automatically when things drop.

## Use Cases
- **Use to just manage connections** - this app makes it easy to manage BT connectiosn, let your other apps use the BT device as normal
- **Self contained TTS solution** - includes optional Music Player Daemon support to allow use of BT devices for TTS without additional app


## Features

- **One-click device management** — scan, pair, connect, and disconnect from the web UI
- **Auto-reconnect** — reconnects after disconnects or reboots with configurable exponential backoff
- **Per-device idle modes** — Power Save (let speaker sleep), Stay Awake (inaudible keep-alive audio), or Auto-Disconnect (timed full disconnect)
- **Per-device MPD instances**:
  -  each BT device gets its own Music Player Daemon
  -  use the MPD integration to expose it as a `media_player` entity in HA for TTS, automations, and volume control
  -  this is not designed for multiroom audio, please use [Multiroom Audio App for HAOS](https://github.com/chrisuthe/Multi-SendSpin-Player-Container)
- **AVRCP media buttons** — per-device toggle for hardware volume buttons and play/pause/skip tracking
- **Multi-adapter support** — detects all Bluetooth adapters with friendly USB device names; switch between them from the UI
- **Real-time monitoring** — live Events view for AVRCP/MPRIS/Transport events, plus a filterable Logs viewer with regex search
- **Dark mode** — automatic system theme detection
- **Safe BLE coexistence** — uses Classic Bluetooth (BR/EDR) only; HA's BLE integrations (sensors, beacons, ESPHome proxies) continue working without interference
- **Security-first** — custom AppArmor profile enforcing least-privilege access, all Bluetooth operations go through BlueZ D-Bus (no raw HCI)



<p align="center">
  <img src="https://github.com/user-attachments/assets/da5e60d5-69ef-41e2-8d8b-27af8fdac120" width="300" />
  &nbsp;&nbsp;&nbsp;
  <img src="https://github.com/user-attachments/assets/64a12aae-c423-49a1-9e2e-48ff310dc9b0" width="550" />
</p>


## Supported Platforms

| Architecture | Examples |
| --- | --- |
| amd64 | Intel NUCs, x86-64 VMs |
| aarch64 (not tested) | Raspberry Pi 4, Raspberry Pi 5 |
| armv7 (not tested) | Raspberry Pi 3 |
| armhf (not tested)| Older ARM devices |

**Requirements:**

- Home Assistant OS (or a setup with D-Bus, PulseAudio, and BlueZ)
- A Bluetooth adapter with BR/EDR (Classic Bluetooth) support — built-in or USB dongle
- Target devices must support A2DP (Advanced Audio Distribution Profile)

## Limitations

- **Single active adapter** — one Bluetooth adapter active at a time (switchable in settings, requires add-on restart)
  - recommended to use a dedicated adapater that is left unmanaged by HAOS, however combined adapter should work, YMMV
- **No multiroom sync** — each speaker is an independent PulseAudio sink; synchronized grouped playback is outside the scope of this add-on, if you want something that does this please consider [Multiroom Audio App for HAOS](https://github.com/chrisuthe/Multi-SendSpin-Player-Container)
- **No audio receiving** — streams audio *to* speakers only. Cannot receive audio *from* Bluetooth devices (e.g., a phone streaming music to HA)
- **No LE Audio / LC3** — Classic A2DP only; Bluetooth Low Energy audio is not supported
- **No HFP/HSP yet** — Hands-Free Profile support is planned but waiting on HAOS audio container changes

## Installation

1. In Home Assistant, go to **Settings > Add-ons > Add-on Store**
2. Click the three-dot menu (top right) and select **Repositories**
3. Add the repository URL:

   ```text
   https://github.com/scyto/ha-bluetooth-audio-manager
   ```

4. Find **Bluetooth Audio Manager** in the store and click **Install**
5. Start the add-on — it appears in the sidebar as **BT Audio**

> **Dev channel:** To install the development build, enable *Show experimental
> add-ons* in your HA profile settings, then install **Bluetooth Audio Manager
> (Dev)** from the store. It tracks the `dev` branch and updates on every push.

## Quick Start

1. Open **BT Audio** from the sidebar
2. Put your speaker in pairing mode
3. Click the **Add Device** tile
4. Click **Pair** next to your device
5. Click **Connect** — the speaker appears as a PulseAudio audio sink
6. Go to **Settings > System > Audio** to select it as the default output
7. Use TTS, media player, or automations to play audio through it

## Configuration

Configuration options and per-device settings are documented in the
[add-on documentation](bluetooth_audio_manager/DOCS.md).

## Links

- [Add-on Documentation](bluetooth_audio_manager/DOCS.md)
- [Dev Documentation](bluetooth_audio_manager_dev/DOCS.md)
- [Report a Device Issue](https://github.com/scyto/ha-bluetooth-audio-manager/issues/new?template=device-issue.yml)
- [All Issues](https://github.com/scyto/ha-bluetooth-audio-manager/issues)
- [Sponsor this project](https://github.com/sponsors/scyto)
