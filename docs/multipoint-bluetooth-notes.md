# Multi-Point Bluetooth: Design Notes

How the add-on interacts with speakers/headphones that support simultaneous connections from multiple A2DP sources (e.g., a phone and this HA system).

## Audio Stack

```
App (keep-alive / MPD / any audio client)
  → pacat / PulseAudio client writes PCM to bluez_sink.{MAC}.a2dp_sink
    → PulseAudio module-bluez5-device detects sink idle→running
      → BlueZ acquires MediaTransport1 fd, sends AVDTP_START to speaker
        → Speaker receives A2DP stream
```

The app never sends AVDTP directly. PulseAudio and BlueZ handle transport negotiation automatically whenever audio data flows into the BlueZ sink.

## What the App Controls

- **Sending audio to the PA sink** — triggers AVDTP_START, which is the mechanism that causes multi-point speakers to switch focus to this source.
- **`resume_sink()` / `suspend_sink()`** — controls whether the PulseAudio→BlueZ transport is active or suspended.
- **Keep-alive streaming** (`keepalive.py`) — sends 1-second bursts of inaudible audio (silence or 2 Hz infrasound) every 5 seconds via `pacat`. Keeps the AVDTP stream active continuously.
- **AVRCP PlaybackStatus** — sets the MPRIS player status to "Playing"/"Paused"/"Stopped". This is an advisory notification to the speaker, not a command. BlueZ sends it via AVRCP when the MediaTransport1 state changes.

## What the Speaker Decides

All of this is firmware behavior — varies by manufacturer:

- Which source gets priority when both are streaming
- Whether to auto-switch when a new AVDTP_START arrives from a different source
- Whether to mix both streams, pause/suspend the other, or ignore the new stream
- Internal source-priority logic (some prefer most-recent, some prefer first-connected)

## What's Invisible to the App

Each source↔sink Bluetooth link is independent and private. From this system's BlueZ, you **cannot** see:

- Whether the speaker is connected to other sources
- Whether another source is currently streaming
- How many A2DP sources the speaker has active
- The speaker's internal source-priority state

`MediaTransport1.State` only reflects the state of **this system's own** A2DP transport — not the phone's or any other source's.

## Practical Scenarios

| Scenario | AVDTP Stream | Speaker Behavior |
|----------|-------------|-----------------|
| **Keep-alive running** | Active continuously (bursts every 5s) | Speaker likely stays locked on to this source |
| **MPD starts playing** | Audio flows → AVDTP_START | Speaker should switch to this source |
| **Idle (no keep-alive)** | No stream | Speaker free to prioritize phone or other sources |
| **Phone starts streaming while app is idle** | No stream from app | Phone gets the speaker; app would need to start audio to compete |
| **Phone starts streaming while keep-alive is active** | Keep-alive still streaming | Speaker firmware decides — most switch to whichever source has "real" audio, but some stay with the existing stream |

## Key Insight

**Sending audio IS the claim mechanism.** There is no separate "switch to me" signal in Bluetooth A2DP. The way to persuade a multi-point speaker to switch focus is to start an AVDTP stream — which happens automatically when any audio flows through the PulseAudio BlueZ sink.

## Possible Future Enhancements

- **"Claim Audio" action** — a UI button that sends a short audio burst + optional AVRCP PLAY to nudge the speaker to switch, for devices without keep-alive enabled.
- **Auto-claim on connect** — always send an initial audio burst when connecting, regardless of keep-alive setting, to establish source priority early.
- **ConnectProfile nudge** — re-asserting `ConnectProfile(A2DP_SINK_UUID)` can sometimes cause devices to re-focus on this source, especially if the transport was idle.
