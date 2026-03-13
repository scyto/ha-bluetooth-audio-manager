# Project Instructions

## Bluetooth A2DP Specification Reference

This project implements Bluetooth A2DP audio management. The official **A2DP v1.4.1 specification** is located at `docs/A2DP_v1.4.1.pdf`. When working on Bluetooth audio features, codec handling, streaming setup, or profile behavior, **always consult this specification** to ensure correctness.

### Key spec sections for quick reference:
- **Section 2.2** — Roles: Source (SRC) sends audio, Sink (SNK) receives audio
- **Section 3** — Application layer: streaming setup and audio streaming procedures
- **Section 4** — Audio codec interoperability requirements:
  - **4.2** — SBC is mandatory (M); MPEG-1,2 Audio, MPEG-2,4 AAC, ATRAC, MPEG-D USAC are optional (O)
  - **4.3** — SBC codec details: sampling frequencies (44.1/48 kHz mandatory for SNK), channel modes, bitpool values, recommended encoder settings (Table 4.7), max bitrate 320 kb/s mono / 512 kb/s stereo
  - **4.5** — AAC codec details: MPEG-2 AAC LC mandatory when AAC supported; MPEG-4 HE-AAC/HE-AACv2/AAC-ELDv2 optional
  - **4.7** — Vendor Specific codecs (aptX, LDAC, etc.) use Vendor ID + Codec ID scheme
- **Section 5** — GAVDP/AVDTP interoperability:
  - **5.1.1** — SRC must support both INT and ACP roles; SNK must support ACP, INT is optional
  - **5.1.3** — Error codes (0xC1–0xE5) for codec configuration errors
  - **5.2.1** — Minimum L2CAP MTU: 335 bytes
  - **5.3** — SDP records: SRC uses "Audio Source" UUID, SNK uses "Audio Sink" UUID; AVDTP version 1.3, A2DP version 1.4
  - **5.5.1** — Class of Device: SNK sets 'Rendering' bit, SRC sets 'Capturing' bit
- **Section 6** — GAP requirements: Connectable mode and Bondable mode mandatory for both SRC and SNK
- **Appendix B** — Full SBC codec technical specification (encoding/decoding)

## BlueZ Reference

BlueZ is the Linux Bluetooth stack this project interacts with via D-Bus (`dbus_next`). When working on device pairing, connection management, profile switching, or adapter control, consult these resources using `WebFetch` or `gh`:

- **Source code**: <https://github.com/bluez/bluez> (GitHub mirror of kernel.org)
- **D-Bus API docs**: <https://github.com/bluez/bluez/tree/master/doc> — the `*.txt` files here define the D-Bus interfaces (adapter-api, device-api, media-api, profile-api, etc.)
- **Issues**: <https://github.com/bluez/bluez/issues>
- **Key interfaces used by this project**:
  - `org.bluez.Adapter1` — adapter discovery, pairing
  - `org.bluez.Device1` — device connect/disconnect, properties (Connected, Paired, UUIDs, etc.)
  - `org.bluez.MediaControl1` / `org.bluez.MediaTransport1` — A2DP transport state
  - `org.bluez.AgentManager1` / `org.bluez.Agent1` — pairing agent

## PulseAudio Reference

PulseAudio handles audio routing on HAOS. When working on sink/source management, card profiles, or audio stream control, consult these resources using `WebFetch`:

- **Source code**: <https://gitlab.freedesktop.org/pulseaudio/pulseaudio>
- **Documentation**: <https://www.freedesktop.org/wiki/Software/PulseAudio/Documentation/>
- **Issues**: <https://gitlab.freedesktop.org/pulseaudio/pulseaudio/-/issues>
- **Bluetooth modules** (most relevant to this project):
  - `module-bluetooth-discover` — auto-loads bluetooth devices
  - `module-bluetooth-policy` — auto-switches profiles
  - `module-bluez5-device` — creates PA cards/sinks for BlueZ devices
- **HAOS runs PulseAudio 17.0** with the native HFP backend (not oFono)

## How to use these references

- **Do not clone** these repos. Use `WebFetch` to read specific files (e.g., `https://raw.githubusercontent.com/bluez/bluez/master/doc/device-api.txt`) or `WebSearch` to find relevant issues/discussions.
- When debugging Bluetooth or audio issues, search the BlueZ and PulseAudio issue trackers for similar reports before assuming a bug is in our code.
- When implementing new D-Bus interactions, always verify the interface/method/property names against the BlueZ D-Bus API docs.
