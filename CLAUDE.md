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
