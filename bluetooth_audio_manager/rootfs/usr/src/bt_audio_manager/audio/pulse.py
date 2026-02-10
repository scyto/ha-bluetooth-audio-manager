"""PulseAudio sink management for Bluetooth A2DP devices.

When BlueZ connects an A2DP device, PulseAudio's module-bluez5-discover
(running in hassio_audio) automatically creates a sink named like:
    bluez_sink.XX_XX_XX_XX_XX_XX.a2dp_sink
"""

import asyncio
import logging

from pulsectl_asyncio import PulseAsync

logger = logging.getLogger(__name__)


class PulseAudioManager:
    """Manages PulseAudio sinks for Bluetooth audio devices."""

    def __init__(self):
        self._pulse: PulseAsync | None = None

    async def connect(self) -> None:
        """Connect to the PulseAudio server.

        The PULSE_SERVER environment variable is set automatically by the
        HA Supervisor when audio: true is configured in config.yaml.
        """
        self._pulse = PulseAsync("bt-audio-manager")
        await self._pulse.connect()
        logger.info("Connected to PulseAudio server")

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
                bt_sinks.append(
                    {
                        "name": sink.name,
                        "description": sink.description,
                        "state": str(sink.state),
                        "volume": sink.volume.value_flat,
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
