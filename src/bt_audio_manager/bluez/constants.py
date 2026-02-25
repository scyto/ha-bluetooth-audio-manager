"""Bluetooth UUID constants for audio profiles."""

# Advanced Audio Distribution Profile (A2DP)
A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"

# Audio/Video Remote Control Profile (AVRCP)
AVRCP_TARGET_UUID = "0000110c-0000-1000-8000-00805f9b34fb"      # A/V Remote Control Target
AVRCP_CONTROLLER_UUID = "0000110e-0000-1000-8000-00805f9b34fb"  # A/V Remote Control Controller
AVRCP_UUID = AVRCP_CONTROLLER_UUID  # backwards compat

# Hands-Free Profile (HFP)
HFP_UUID = "0000111e-0000-1000-8000-00805f9b34fb"

# Headset Profile (HSP) — older mono profile, BlueZ treats same as HFP
HSP_UUID = "00001108-0000-1000-8000-00805f9b34fb"

# BlueZ D-Bus service and interface names
BLUEZ_SERVICE = "org.bluez"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
DEVICE_INTERFACE = "org.bluez.Device1"
AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
MEDIA_TRANSPORT_INTERFACE = "org.bluez.MediaTransport1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"

# Default adapter path
DEFAULT_ADAPTER_PATH = "/org/bluez/hci0"

# Agent capability for headless audio pairing (Just Works)
AGENT_CAPABILITY = "NoInputNoOutput"
AGENT_PATH = "/org/ha/bluetooth_audio/agent"

# AVRCP media player (registered with BlueZ to receive speaker button events)
MEDIA_INTERFACE = "org.bluez.Media1"
PLAYER_PATH = "/org/ha/bluetooth_audio/player"

# LE Audio (Bluetooth 5.2+) — not yet supported by this add-on
PACS_UUID = "00001850-0000-1000-8000-00805f9b34fb"   # Published Audio Capabilities
ASCS_UUID = "0000184e-0000-1000-8000-00805f9b34fb"   # Audio Stream Control
LE_AUDIO_UUIDS = frozenset({PACS_UUID, ASCS_UUID})

# Audio-capable device UUIDs (any of these indicate audio support)
AUDIO_UUIDS = frozenset({
    A2DP_SINK_UUID, A2DP_SOURCE_UUID, AVRCP_TARGET_UUID,
    AVRCP_CONTROLLER_UUID, HFP_UUID, HSP_UUID,
    PACS_UUID, ASCS_UUID,
})

# UUIDs that indicate a device can receive/play audio (used to filter discovery)
# Excludes A2DP Source (phone sending audio), AVRCP-only, and LE Audio devices.
SINK_UUIDS = frozenset({A2DP_SINK_UUID, HFP_UUID, HSP_UUID})

# Feature flag: HFP profile switching is disabled until the HAOS audio
# container supports SCO sockets (AF_BLUETOOTH).  See issue #98.
HFP_SWITCHING_ENABLED = False

# ── Bluetooth Class of Device (CoD) helpers ──────────────────────────
# The 24-bit CoD field encodes Major Device Class in bits 12-8.
# See Bluetooth Assigned Numbers § 2.8.
COD_MAJOR_AUDIO = 0x04

_COD_MAJOR_LABELS = {
    0x00: "Misc",
    0x01: "Computer",
    0x02: "Phone",
    0x03: "LAN/AP",
    COD_MAJOR_AUDIO: "Audio/Video",
    0x05: "Peripheral",
    0x06: "Imaging",
    0x07: "Wearable",
    0x08: "Toy",
    0x09: "Health",
}


def cod_major_class(cod: int) -> int:
    """Extract Major Device Class from a raw CoD value (bits 12-8)."""
    return (cod >> 8) & 0x1F


def cod_minor_class(cod: int) -> int:
    """Extract Minor Device Class from a raw CoD value (bits 7-2)."""
    return (cod >> 2) & 0x3F


def cod_major_label(cod: int) -> str:
    """Return a human-readable label for the Major Device Class."""
    return _COD_MAJOR_LABELS.get(cod_major_class(cod), "Unknown")


# Audio/Video minor classes that can receive/play audio.
# Excludes: 0 (Uncategorized), 4 (Microphone), 9 (Set-top box),
# 11 (VCR), 12 (Video Camera), 13 (Camcorder), 14 (Video Monitor),
# 18 (Gaming/Toy).
COD_AUDIO_SINK_MINORS = frozenset({
    1,   # Wearable Headset
    2,   # Hands-free Device
    5,   # Loudspeaker
    6,   # Headphones
    7,   # Portable Audio
    8,   # Car Audio
    10,  # HiFi Audio Device
    15,  # Video Display and Loudspeaker
    16,  # Video Conferencing
})


def is_cod_audio_sink(cod: int) -> bool:
    """Return True if CoD indicates an audio sink device.

    Used as a fallback for devices that don't advertise UUIDs.
    """
    return (
        cod_major_class(cod) == COD_MAJOR_AUDIO
        and cod_minor_class(cod) in COD_AUDIO_SINK_MINORS
    )
