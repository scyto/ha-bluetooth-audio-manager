"""Keep-alive audio streaming to prevent Bluetooth speaker auto-shutdown.

Many Bluetooth speakers enter standby after 30-120 seconds of silence.
This service streams inaudible audio to keep the connection alive.

Two methods:
- "silence": PCM zeros. Minimal CPU. Some speakers detect digital silence
  and still enter standby.
- "infrasound": 2 Hz sine wave at very low amplitude (below human hearing
  threshold of ~20 Hz). Fools silence detection in most speakers.
  Approach from adrgumula/HomeAssistantBluetoothSpeaker.
"""

import asyncio
import logging
import math
import struct

logger = logging.getLogger(__name__)

SAMPLE_RATE = 44100
CHANNELS = 1
STREAM_DURATION = 1.0  # seconds per burst
STREAM_INTERVAL = 5.0  # seconds between bursts


class KeepAliveService:
    """Streams inaudible audio to a Bluetooth sink to prevent auto-shutdown."""

    def __init__(self, method: str = "infrasound"):
        self._method = method
        self._target_sink: str | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    def set_target_sink(self, sink_name: str) -> None:
        """Set the PulseAudio sink to stream keep-alive audio to."""
        self._target_sink = sink_name
        logger.info("Keep-alive target set to %s", sink_name)

    async def start(self) -> None:
        """Start the keep-alive streaming loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._stream_loop())
        logger.info("Keep-alive service started (method=%s)", self._method)

    async def stop(self) -> None:
        """Stop the keep-alive streaming loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Keep-alive service stopped")

    async def _stream_loop(self) -> None:
        """Periodically stream a short burst of inaudible audio via pacat."""
        pcm_data = self._generate_audio()

        while self._running:
            if self._target_sink:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "pacat",
                        "--device", self._target_sink,
                        "--format=s16le",
                        f"--rate={SAMPLE_RATE}",
                        f"--channels={CHANNELS}",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    proc.stdin.write(pcm_data)
                    proc.stdin.close()
                    await proc.wait()
                except Exception as e:
                    logger.debug("Keep-alive stream error: %s", e)

            await asyncio.sleep(STREAM_INTERVAL)

    def _generate_audio(self) -> bytes:
        """Generate the keep-alive audio buffer."""
        if self._method == "silence":
            return self._generate_silence()
        return self._generate_infrasound()

    @staticmethod
    def _generate_silence() -> bytes:
        """Generate PCM silence (all zeros)."""
        num_samples = int(SAMPLE_RATE * STREAM_DURATION)
        return b"\x00\x00" * num_samples

    @staticmethod
    def _generate_infrasound(freq: float = 2.0, amplitude: int = 100) -> bytes:
        """Generate a 2 Hz sine wave at very low amplitude.

        At 2 Hz, the signal is well below the human hearing threshold (~20 Hz).
        Amplitude of 100 out of 32767 is -50 dB, effectively inaudible even
        if a speaker could reproduce it.
        """
        num_samples = int(SAMPLE_RATE * STREAM_DURATION)
        data = bytearray(num_samples * 2)
        for i in range(num_samples):
            value = int(amplitude * math.sin(2.0 * math.pi * freq * i / SAMPLE_RATE))
            struct.pack_into("<h", data, i * 2, value)
        return bytes(data)
