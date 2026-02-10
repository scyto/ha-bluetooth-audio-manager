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

# Regex for parsing "pactl list sinks" sample spec line,
# e.g. "s16le 2ch 48000Hz"
_SPEC_SUFFIX_HZ = "Hz"
_SPEC_SUFFIX_CH = "ch"


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

    async def _pactl_sample_specs(self) -> dict[str, dict]:
        """Parse sample specs from ``pactl list sinks``.

        pulsectl's ctypes wrapper returns garbage for the sample_spec
        struct on bluez sinks (struct alignment / wire-protocol mismatch),
        so we shell out to pactl which deserializes correctly.

        Returns a dict keyed by sink name, e.g.
        ``{"bluez_sink.XX.a2dp_sink": {"format": "s16le", "rate": 48000, "channels": 2}}``
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl", "list", "sinks",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return {}
        except (FileNotFoundError, OSError) as exc:
            logger.debug("pactl not available: %s", exc)
            return {}

        specs: dict[str, dict] = {}
        current_name: str | None = None
        for line in stdout.decode(errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("Name:"):
                current_name = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Sample Specification:") and current_name:
                # e.g. "s16le 2ch 48000Hz"
                spec_str = stripped.split(":", 1)[1].strip()
                fmt = None
                rate = None
                channels = None
                for part in spec_str.split():
                    if part.endswith(_SPEC_SUFFIX_HZ):
                        try:
                            rate = int(part[: -len(_SPEC_SUFFIX_HZ)])
                        except ValueError:
                            pass
                    elif part.endswith(_SPEC_SUFFIX_CH):
                        try:
                            channels = int(part[: -len(_SPEC_SUFFIX_CH)])
                        except ValueError:
                            pass
                    else:
                        fmt = part
                specs[current_name] = {
                    "format": fmt,
                    "rate": rate,
                    "channels": channels,
                }
        return specs

    async def list_bt_sinks(self) -> list[dict]:
        """List all Bluetooth A2DP sinks currently available."""
        sinks = await self._pulse.sink_list()
        sample_specs = await self._pactl_sample_specs()
        bt_sinks = []
        for sink in sinks:
            if "bluez" in sink.name.lower():
                # Extract human-readable state from pulsectl enum
                state_name = getattr(sink.state, "name", None)
                if state_name is None:
                    # Fallback: parse "<EnumValue sink/source-state=idle>"
                    raw = str(sink.state)
                    state_name = raw.split("=")[-1].rstrip(">") if "=" in raw else raw

                # Sample spec from pactl (reliable) instead of pulsectl ctypes
                spec = sample_specs.get(sink.name, {})

                bt_sinks.append(
                    {
                        "name": sink.name,
                        "description": sink.description,
                        "state": state_name,
                        "volume": round(sink.volume.value_flat * 100),
                        "mute": sink.mute,
                        "sample_rate": spec.get("rate"),
                        "channels": spec.get("channels"),
                        "format": spec.get("format"),
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
