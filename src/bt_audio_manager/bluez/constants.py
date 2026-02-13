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

# Audio-capable device UUIDs (any of these indicate audio support)
AUDIO_UUIDS = frozenset({A2DP_SINK_UUID, A2DP_SOURCE_UUID, AVRCP_TARGET_UUID, AVRCP_CONTROLLER_UUID, HFP_UUID, HSP_UUID})

# UUIDs that indicate a device can receive/play audio (used to filter discovery)
# Excludes A2DP Source (phone sending audio) and AVRCP-only devices.
SINK_UUIDS = frozenset({A2DP_SINK_UUID, HFP_UUID, HSP_UUID})
