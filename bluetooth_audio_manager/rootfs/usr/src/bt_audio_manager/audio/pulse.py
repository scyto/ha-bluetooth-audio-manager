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
        self._server: str | None = None  # resolved PA server address
        self._subscribe_task: asyncio.Task | None = None
        self._volume_callback = None

    async def connect(self) -> None:
        """Connect to the PulseAudio server.

        Tries PULSE_SERVER env var first, then known HAOS socket paths.
        """
        # If PULSE_SERVER is set, try it directly
        if os.environ.get("PULSE_SERVER"):
            self._pulse = PulseAsync("bt-audio-manager")
            await self._pulse.connect()
            self._server = os.environ["PULSE_SERVER"]
            logger.info(
                "Connected to PulseAudio via PULSE_SERVER=%s",
                self._server,
            )
            return

        # Try fallback addresses
        logger.info("PULSE_SERVER not set, probing known HAOS audio paths...")
        for server in _FALLBACK_SERVERS:
            try:
                os.environ["PULSE_SERVER"] = server
                self._pulse = PulseAsync("bt-audio-manager")
                await self._pulse.connect()
                self._server = server
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
        await self.stop_event_monitor()
        if self._pulse:
            self._pulse.close()
            self._pulse = None

    def on_volume_change(self, callback) -> None:
        """Register a callback for Bluetooth sink volume changes.

        Callback signature: ``callback(sink_name: str, volume: int, mute: bool)``
        """
        self._volume_callback = callback

    async def start_event_monitor(self) -> None:
        """Subscribe to PulseAudio sink events via pulsectl_asyncio.

        Uses a dedicated second PulseAsync connection (the primary one
        can't be shared because ``subscribe_events`` blocks it).
        Detects AVRCP Absolute Volume changes on Bluetooth sinks.
        """
        if self._subscribe_task and not self._subscribe_task.done():
            return
        self._subscribe_task = asyncio.create_task(self._event_monitor_loop())

    async def stop_event_monitor(self) -> None:
        """Cancel the PulseAudio event subscription task."""
        if self._subscribe_task and not self._subscribe_task.done():
            self._subscribe_task.cancel()
            try:
                await self._subscribe_task
            except asyncio.CancelledError:
                pass
            self._subscribe_task = None

    async def _event_monitor_loop(self) -> None:
        """Subscribe to sink events and log Bluetooth volume changes."""
        try:
            # Second connection dedicated to event subscription
            async with PulseAsync("bt-audio-events") as pulse_events:
                logger.info("PA event subscription started (sink events)")
                async for event in pulse_events.subscribe_events("sink", "server"):
                    if event.t == "change" and self._pulse:
                        try:
                            sink = await self._pulse.sink_info(event.index)
                            if "bluez" in sink.name.lower():
                                vol = round(sink.volume.value_flat * 100)
                                logger.info(
                                    "PA sink volume change: %s vol=%d%% mute=%s state=%s",
                                    sink.name, vol, sink.mute,
                                    getattr(sink.state, "name", sink.state),
                                )
                                if self._volume_callback:
                                    self._volume_callback(sink.name, vol, sink.mute)
                        except Exception as e:
                            logger.debug("PA event handler error: %s", e)
                    elif event.t in ("new", "remove"):
                        logger.info("PA sink %s: index=%d", event.t, event.index)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("PA event subscription error: %s", e)

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

    async def reload_bluez_discover(self) -> bool:
        """Unload and reload PulseAudio's module-bluez5-discover.

        Forces PA to re-enumerate all BlueZ transports and create sinks
        for any it missed (e.g. transports that existed before the module
        started or were recreated without a proper InterfacesAdded signal).

        Returns True if the reload succeeded.
        """
        try:
            # Find the module index for module-bluez5-discover
            proc = await asyncio.create_subprocess_exec(
                "pactl", "list", "modules", "short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                logger.warning("pactl list modules failed (rc=%d)", proc.returncode)
                return False

            module_ids = []
            for line in stdout.decode(errors="replace").splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and "module-bluez5-discover" in parts[1]:
                    module_ids.append(parts[0])

            if not module_ids:
                logger.warning("module-bluez5-discover not found in PulseAudio")
                return False

            # Unload existing module(s)
            for mid in module_ids:
                logger.info("Unloading PA module-bluez5-discover (id=%s)", mid)
                proc = await asyncio.create_subprocess_exec(
                    "pactl", "unload-module", mid,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()

            await asyncio.sleep(1)

            # Reload the module
            logger.info("Loading PA module-bluez5-discover")
            proc = await asyncio.create_subprocess_exec(
                "pactl", "load-module", "module-bluez5-discover",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(
                    "pactl load-module failed (rc=%d): %s",
                    proc.returncode, stderr.decode(errors="replace"),
                )
                return False

            logger.info("PA module-bluez5-discover reloaded successfully")
            return True
        except (FileNotFoundError, OSError) as exc:
            logger.warning("pactl not available for module reload: %s", exc)
            return False

    async def get_sink_for_address(self, address: str) -> str | None:
        """Get the current sink name for a Bluetooth address, if it exists."""
        addr_underscored = address.replace(":", "_")
        pattern = f"bluez_sink.{addr_underscored}"
        sinks = await self._pulse.sink_list()
        for sink in sinks:
            if pattern in sink.name:
                return sink.name
        return None
