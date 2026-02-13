"""PulseAudio sink management for Bluetooth A2DP devices.

When BlueZ connects an A2DP device, PulseAudio's module-bluez5-discover
(running in hassio_audio) automatically creates a sink named like:
    bluez_sink.XX_XX_XX_XX_XX_XX.a2dp_sink
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

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
        self._state_callback = None
        self._idle_callback = None

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

    async def reconnect(self, retries: int = 10, delay: float = 2.0) -> None:
        """Reconnect to PulseAudio after the audio service restarts.

        Closes the old connection and retries until PA is back.
        The event monitor is restarted automatically on success.
        """
        if self._pulse:
            try:
                self._pulse.close()
            except Exception:
                pass
            self._pulse = None

        for attempt in range(1, retries + 1):
            try:
                self._pulse = PulseAsync("bt-audio-manager")
                await self._pulse.connect()
                logger.info("Reconnected to PulseAudio (attempt %d)", attempt)
                await self.start_event_monitor()
                return
            except Exception:
                if self._pulse:
                    try:
                        self._pulse.close()
                    except Exception:
                        pass
                    self._pulse = None
                if attempt < retries:
                    await asyncio.sleep(delay)

        raise ConnectionError("PulseAudio not reachable after audio restart")

    def on_volume_change(self, callback) -> None:
        """Register a callback for Bluetooth sink volume changes.

        Callback signature: ``callback(sink_name: str, volume: int, mute: bool)``
        """
        self._volume_callback = callback

    def on_sink_state_change(self, callback) -> None:
        """Register a callback for Bluetooth sink state transitions to 'running'.

        Fires when a BT sink transitions to 'running' (audio actively flowing).
        Callback signature: ``callback(sink_name: str)``
        """
        self._state_callback = callback

    def on_sink_idle(self, callback) -> None:
        """Register a callback for Bluetooth sink state transitions to 'idle'.

        Fires when a BT sink transitions from 'running' to 'idle' or 'suspended'
        (audio stopped flowing).
        Callback signature: ``callback(sink_name: str)``
        """
        self._idle_callback = callback

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
        """Subscribe to sink events and log Bluetooth volume changes.

        Auto-restarts with exponential backoff if the PA connection drops
        (e.g. after a module-bluez5-discover reload).
        """
        retry_delay = 2
        while True:
            bt_sink_states: dict[str, str] = {}
            try:
                async with PulseAsync("bt-audio-events") as pulse_events:
                    retry_delay = 2  # reset on successful connection
                    logger.info("PA event subscription started (sink events)")
                    async for event in pulse_events.subscribe_events("sink", "server"):
                        if event.t == "change" and self._pulse:
                            try:
                                sink = await self._pulse.sink_info(event.index)
                                if "bluez" in sink.name.lower():
                                    vol = round(sink.volume.value_flat * 100)
                                    state_name = getattr(sink.state, "name", str(sink.state))
                                    logger.info(
                                        "PA sink volume change: %s vol=%d%% mute=%s state=%s",
                                        sink.name, vol, sink.mute, state_name,
                                    )
                                    if self._volume_callback:
                                        self._volume_callback(sink.name, vol, sink.mute)
                                    # Detect state transitions
                                    prev_state = bt_sink_states.get(sink.name)
                                    bt_sink_states[sink.name] = state_name
                                    if state_name == "running" and prev_state != "running":
                                        logger.info("BT sink %s → running (was %s)", sink.name, prev_state)
                                        if self._state_callback:
                                            self._state_callback(sink.name)
                                    elif state_name != "running" and prev_state == "running":
                                        logger.info("BT sink %s → %s (was running)", sink.name, state_name)
                                        if self._idle_callback:
                                            self._idle_callback(sink.name)
                            except Exception as e:
                                logger.debug("PA event handler error: %s", e)
                        elif event.t in ("new", "remove"):
                            logger.info("PA sink %s: index=%d", event.t, event.index)
            except asyncio.CancelledError:
                return  # clean shutdown
            except Exception as e:
                logger.warning(
                    "PA event subscription error: %s — restarting in %ds", e, retry_delay,
                )
                try:
                    await asyncio.sleep(retry_delay)
                except asyncio.CancelledError:
                    return
                retry_delay = min(retry_delay * 2, 30)

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
        self,
        address: str,
        timeout: float = 15.0,
        connected_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> str | None:
        """Wait for PulseAudio to register a Bluetooth sink for a given address.

        Matches both A2DP (``bluez_sink.XX.a2dp_sink``) and HFP
        (``bluez_sink.XX.headset_head_unit``) sinks.

        If *connected_check* is provided, it is awaited each iteration to
        bail out early when the device disconnects mid-wait.
        """
        addr_underscored = address.replace(":", "_")
        expected_pattern = f"bluez_sink.{addr_underscored}"

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            sinks = await self._pulse.sink_list()
            for sink in sinks:
                if expected_pattern in sink.name:
                    logger.info("BT sink ready: %s", sink.name)
                    return sink.name
            if connected_check and not await connected_check():
                logger.warning(
                    "Device %s disconnected while waiting for BT sink", address
                )
                return None
            await asyncio.sleep(1.0)

        logger.warning(
            "BT sink for %s did not appear within %ss", address, timeout
        )
        return None

    async def set_default_sink(self, sink_name: str) -> None:
        """Set a specific sink as the PulseAudio default output."""
        await self._pulse.sink_default_set(sink_name)
        logger.info("Default audio output set to %s", sink_name)

    async def activate_bt_card_profile(self, address: str, profile: str = "a2dp") -> bool:
        """Activate a Bluetooth PA card profile for a specific device.

        Uses ``pactl set-card-profile`` to tell PulseAudio to create a sink
        for a specific device.  Per-device safe — does not affect other
        Bluetooth audio connections.

        Args:
            address: Bluetooth MAC address.
            profile: ``"a2dp"`` for stereo music, ``"hfp"`` for mono + mic.

        Returns True if the profile was activated successfully.
        """
        card_name = "bluez_card." + address.replace(":", "_")

        if profile == "hfp":
            # HFP/HSP: BlueZ uses "headset-head-unit", PipeWire may use "headset_head_unit"
            candidates = ("headset-head-unit", "headset_head_unit")
        else:
            # A2DP: PA uses "a2dp-sink", PipeWire may use "a2dp_sink"
            candidates = ("a2dp-sink", "a2dp_sink")

        try:
            for pa_profile in candidates:
                proc = await asyncio.create_subprocess_exec(
                    "pactl", "set-card-profile", card_name, pa_profile,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode == 0:
                    logger.info("PA card profile set: %s -> %s", card_name, pa_profile)
                    return True
                logger.debug(
                    "set-card-profile %s %s failed: %s",
                    card_name, pa_profile, stderr.decode(errors="replace").strip(),
                )

            if profile == "hfp":
                # For HFP: cycling to "off" destroys the card and the
                # headset-head-unit profile won't come back (PA needs its
                # HFP handler registered with BlueZ).  Fail fast so the
                # caller can retry after ensuring HFP is connected.
                logger.warning(
                    "PA card %s has no HFP profile — PulseAudio's HFP handler "
                    "may not be registered with BlueZ",
                    card_name,
                )
            else:
                # A2DP: cycle off → target to force recreation
                logger.info("Cycling PA card profile for %s (off -> %s)...", card_name, profile)
                proc = await asyncio.create_subprocess_exec(
                    "pactl", "set-card-profile", card_name, "off",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                await asyncio.sleep(1)

                for pa_profile in candidates:
                    proc = await asyncio.create_subprocess_exec(
                        "pactl", "set-card-profile", card_name, pa_profile,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()
                    if proc.returncode == 0:
                        logger.info("PA card profile cycled: %s -> %s", card_name, pa_profile)
                        return True

                logger.warning("PA card %s not found or profile activation failed", card_name)
            return False
        except (FileNotFoundError, OSError) as exc:
            logger.warning("pactl not available: %s", exc)
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

    async def get_sink_volume(self, sink_name: str) -> tuple[int, str] | None:
        """Get (volume_pct, state_name) for a specific sink.

        Returns None if the sink is not found.
        """
        try:
            sinks = await self._pulse.sink_list()
            for sink in sinks:
                if sink.name == sink_name:
                    vol = round(sink.volume.value_flat * 100)
                    state_name = getattr(sink.state, "name", None)
                    if state_name is None:
                        raw = str(sink.state)
                        state_name = raw.split("=")[-1].rstrip(">") if "=" in raw else raw
                    return (vol, state_name)
        except Exception as e:
            logger.debug("get_sink_volume(%s) failed: %s", sink_name, e)
        return None

    async def suspend_sink(self, sink_name: str) -> bool:
        """Suspend a sink to release the A2DP transport."""
        if not self._pulse:
            return False
        try:
            sinks = await self._pulse.sink_list()
            for sink in sinks:
                if sink.name == sink_name:
                    await self._pulse.sink_suspend(sink.index, suspend=True)
                    logger.info("Suspended PA sink: %s", sink_name)
                    return True
            logger.warning("Sink not found for suspend: %s", sink_name)
        except Exception as e:
            logger.warning("Failed to suspend sink %s: %s", sink_name, e)
        return False

    async def resume_sink(self, sink_name: str) -> bool:
        """Resume a previously suspended sink."""
        if not self._pulse:
            return False
        try:
            sinks = await self._pulse.sink_list()
            for sink in sinks:
                if sink.name == sink_name:
                    await self._pulse.sink_suspend(sink.index, suspend=False)
                    logger.info("Resumed PA sink: %s", sink_name)
                    return True
            logger.warning("Sink not found for resume: %s", sink_name)
        except Exception as e:
            logger.warning("Failed to resume sink %s: %s", sink_name, e)
        return False

    async def set_sink_volume(self, sink_name: str, volume_pct: int) -> bool:
        """Set PulseAudio sink volume (0-100%).

        Uses ``pactl set-sink-volume`` which propagates to AVRCP Absolute
        Volume on Bluetooth sinks — changing the speaker's hardware level.

        Returns True if the command succeeded.
        """
        vol_str = f"{max(0, min(100, volume_pct))}%"
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl", "set-sink-volume", sink_name, vol_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                logger.info("PA sink volume set: %s → %s", sink_name, vol_str)
                return True
            logger.warning(
                "pactl set-sink-volume %s %s failed: %s",
                sink_name, vol_str, stderr.decode(errors="replace").strip(),
            )
            return False
        except (FileNotFoundError, OSError) as exc:
            logger.warning("pactl not available: %s", exc)
            return False
