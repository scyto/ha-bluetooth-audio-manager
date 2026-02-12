"""MPD (Music Player Daemon) management for Bluetooth audio playback.

Embeds an MPD instance that outputs through PulseAudio to the connected
Bluetooth speaker.  Provides a bridge from AVRCP/MPRIS speaker button
commands to MPD playback control via python-mpd2.

HA's built-in MPD integration connects to port 6600 to create a
media_player entity.
"""

import asyncio
import logging
import os
import textwrap

from mpd.asyncio import MPDClient

logger = logging.getLogger(__name__)

MPD_CONF_PATH = "/tmp/mpd.conf"
MPD_DATA_DIR = "/data/mpd"
MPD_MUSIC_DIR = "/data/mpd/music"
MPD_DB_FILE = "/data/mpd/database"
MPD_STATE_FILE = "/data/mpd/state"
MPD_PID_FILE = "/tmp/mpd.pid"
MPD_HOST = "127.0.0.1"
MPD_PORT = 6600


class MPDManager:
    """Manages an embedded MPD daemon and bridges AVRCP commands to it."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._client: MPDClient | None = None
        self._running = False
        self._connect_lock = asyncio.Lock()
        self._sink_name: str | None = None
        self._stderr_task: asyncio.Task | None = None

    # -- Lifecycle --

    async def start(self, sink_name: str) -> None:
        """Generate config, start MPD daemon, connect client.

        Args:
            sink_name: PulseAudio sink to target (e.g. bluez_sink.XX_XX.a2dp_sink).
        """
        if self._running:
            return

        self._sink_name = sink_name
        os.makedirs(MPD_MUSIC_DIR, exist_ok=True)
        self._generate_config()
        await self._start_daemon()
        await self._connect_client()
        self._running = True
        logger.info("MPD started (port %d)", MPD_PORT)

    async def stop(self) -> None:
        """Disconnect client and terminate MPD daemon."""
        self._running = False

        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None

        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
            logger.info("MPD daemon stopped")
        self._process = None

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None

    # -- Config generation --

    def _generate_config(self) -> None:
        """Write a minimal mpd.conf targeting a specific PulseAudio sink."""
        config = textwrap.dedent("""\
            music_directory     "{music_dir}"
            db_file             "{db_file}"
            state_file          "{state_file}"
            pid_file            "{pid_file}"
            bind_to_address     "0.0.0.0"
            port                "{port}"
            log_level           "verbose"
            auto_update         "no"

            audio_output {{
                type    "pulse"
                name    "Bluetooth Speaker"
                sink    "{sink}"
            }}

            input {{
                plugin  "curl"
            }}
        """).format(
            music_dir=MPD_MUSIC_DIR,
            db_file=MPD_DB_FILE,
            state_file=MPD_STATE_FILE,
            pid_file=MPD_PID_FILE,
            port=MPD_PORT,
            sink=self._sink_name,
        )

        with open(MPD_CONF_PATH, "w") as f:
            f.write(config)
        logger.debug("MPD config written to %s", MPD_CONF_PATH)

    # -- Daemon management --

    async def _start_daemon(self) -> None:
        """Start MPD in foreground mode as a subprocess."""
        self._process = await asyncio.create_subprocess_exec(
            "mpd", "--no-daemon", "--stderr", MPD_CONF_PATH,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        # Give MPD a moment to initialize
        await asyncio.sleep(0.5)
        if self._process.returncode is not None:
            stderr = await self._process.stderr.read()
            raise RuntimeError(f"MPD failed to start: {stderr.decode().strip()}")
        logger.info("MPD daemon started (pid=%d)", self._process.pid)
        # Stream MPD's stderr to our logger so errors are visible
        self._stderr_task = asyncio.create_task(self._stream_stderr())

    async def _stream_stderr(self) -> None:
        """Read MPD stderr line by line and forward to our logger."""
        try:
            while self._process and self._process.stderr:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode().rstrip()
                if text:
                    logger.info("[mpd] %s", text)
        except Exception:
            pass

    # -- Client connection --

    async def _connect_client(self) -> None:
        """Connect the python-mpd2 async client."""
        self._client = MPDClient()
        for attempt in range(5):
            try:
                await self._client.connect(MPD_HOST, MPD_PORT)
                logger.debug("MPD client connected")
                return
            except (ConnectionRefusedError, OSError):
                if attempt < 4:
                    await asyncio.sleep(0.5)
        logger.warning("Could not connect MPD client after retries")
        self._client = None

    async def _ensure_connected(self) -> None:
        """Reconnect client if the connection was lost."""
        if self._client:
            try:
                await self._client.ping()
                return
            except Exception:
                self._client = None

        async with self._connect_lock:
            if self._client:
                return
            await self._connect_client()

    # -- AVRCP command bridge --

    async def handle_command(self, command: str, detail: str) -> None:
        """Forward an AVRCP/MPRIS command to MPD."""
        await self._ensure_connected()
        if not self._client:
            return

        try:
            if command == "Play":
                await self._client.play()
            elif command == "Pause":
                await self._client.pause(1)
            elif command == "PlayPause":
                status = await self._client.status()
                if status.get("state") == "play":
                    await self._client.pause(1)
                else:
                    await self._client.play()
            elif command == "Stop":
                await self._client.stop()
            elif command == "Next":
                await self._client.next()
            elif command == "Previous":
                await self._client.previous()
            elif command == "Volume":
                vol_str = detail.rstrip("%").split(".")[0]
                try:
                    await self._client.setvol(int(vol_str))
                except ValueError:
                    pass
        except Exception as e:
            logger.warning("MPD command %s failed: %s", command, e)
            self._client = None

    async def set_volume(self, vol_pct: int) -> None:
        """Set MPD volume (0-100)."""
        await self._ensure_connected()
        if not self._client:
            return
        try:
            await self._client.setvol(max(0, min(100, vol_pct)))
        except Exception as e:
            logger.debug("MPD set_volume failed: %s", e)
            self._client = None

    async def get_status(self) -> dict:
        """Return MPD status dict."""
        await self._ensure_connected()
        if not self._client:
            return {"state": "unknown"}
        try:
            return await self._client.status()
        except Exception:
            self._client = None
            return {"state": "unknown"}
