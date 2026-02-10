"""PulseAudio sink management for Bluetooth A2DP devices.

When BlueZ connects an A2DP device, PulseAudio's module-bluez5-discover
(running in hassio_audio) automatically creates a sink named like:
    bluez_sink.XX_XX_XX_XX_XX_XX.a2dp_sink
"""

import asyncio
import logging
import os

from pulsectl_asyncio import PulseAsync

logger = logging.getLogger(__name__)

# Known PulseAudio server addresses in HAOS (tried in order).
# The Supervisor mounts the audio socket at /run/audio/ when audio: true.
_FALLBACK_SERVERS = [
    "unix:/run/audio/pulse.sock",
    "unix:/run/audio/native",
]


class PulseAudioManager:
    """Manages PulseAudio sinks for Bluetooth audio devices."""

    def __init__(self):
        self._pulse: PulseAsync | None = None

    async def connect(self) -> None:
        """Connect to the PulseAudio server.

        Tries PULSE_SERVER env var first, then known HAOS socket paths.
        """
        # If PULSE_SERVER is set, try it directly
        if os.environ.get("PULSE_SERVER"):
            self._pulse = PulseAsync("bt-audio-manager")
            await self._pulse.connect()
            logger.info(
                "Connected to PulseAudio via PULSE_SERVER=%s",
                os.environ["PULSE_SERVER"],
            )
            return

        # Try fallback addresses
        logger.info("PULSE_SERVER not set, probing known HAOS audio paths...")
        for server in _FALLBACK_SERVERS:
            try:
                os.environ["PULSE_SERVER"] = server
                self._pulse = PulseAsync("bt-audio-manager")
                await self._pulse.connect()
                logger.info("Connected to PulseAudio via %s", server)
                return
            except Exception:
                logger.debug("PulseAudio not available at %s", server)
                if self._pulse:
                    self._pulse.close()
                    self._pulse = None

        # None worked — clean up and raise
        os.environ.pop("PULSE_SERVER", None)
        raise ConnectionError(
            "PulseAudio not reachable at any known address. "
            "Check that 'audio: true' is set in config.yaml and "
            "the HA audio service is running."
        )

    async def disconnect(self) -> None:
        """Disconnect from PulseAudio."""
        if self._pulse:
            self._pulse.close()
            self._pulse = None

    async def list_bt_sinks(self) -> list[dict]:
        """List all Bluetooth A2DP sinks currently available."""
        sinks = await self._pulse.sink_list()
        bt_sinks = []
        for sink in sinks:
            if "bluez" in sink.name.lower():
                # Extract human-readable state from pulsectl enum
                state_name = getattr(sink.state, "name", None)
                if state_name is None:
                    # Fallback: parse "<EnumValue sink/source-state=idle>"
                    raw = str(sink.state)
                    state_name = raw.split("=")[-1].rstrip(">") if "=" in raw else raw

                # Sample spec info
                sample_spec = getattr(sink, "sample_spec", None)
                sample_rate = getattr(sample_spec, "rate", None)
                channels = getattr(sample_spec, "channels", None)
                sample_format = getattr(sample_spec, "format", None)

                bt_sinks.append(
                    {
                        "name": sink.name,
                        "description": sink.description,
                        "state": state_name,
                        "volume": round(sink.volume.value_flat * 100),
                        "mute": sink.mute,
                        "sample_rate": sample_rate,
                        "channels": channels,
                        "format": str(sample_format) if sample_format else None,
                    }
                )
        return bt_sinks

    async def wait_for_bt_sink(
        self, address: str, timeout: float = 15.0
    ) -> str | None:
        """Wait for PulseAudio to register the A2DP sink for a given address.

        PulseAudio sink names use the MAC with underscores:
            bluez_sink.XX_XX_XX_XX_XX_XX.a2dp_sink
        """
        addr_underscored = address.replace(":", "_")
        expected_pattern = f"bluez_sink.{addr_underscored}"

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            sinks = await self._pulse.sink_list()
            for sink in sinks:
                if expected_pattern in sink.name:
                    logger.info("A2DP sink ready: %s", sink.name)
                    return sink.name
            await asyncio.sleep(1.0)

        logger.warning(
            "A2DP sink for %s did not appear within %ss", address, timeout
        )
        return None

    async def set_default_sink(self, sink_name: str) -> None:
        """Set a specific sink as the PulseAudio default output."""
        await self._pulse.sink_default_set(sink_name)
        logger.info("Default audio output set to %s", sink_name)

    async def get_sink_for_address(self, address: str) -> str | None:
        """Get the current sink name for a Bluetooth address, if it exists."""
        addr_underscored = address.replace(":", "_")
        pattern = f"bluez_sink.{addr_underscored}"
        sinks = await self._pulse.sink_list()
        for sink in sinks:
            if pattern in sink.name:
                return sink.name
        return None
