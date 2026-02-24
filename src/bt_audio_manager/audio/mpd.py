"""MPD (Music Player Daemon) management for Bluetooth audio playback.

Embeds one MPD instance per Bluetooth speaker, each on a unique port
(6600-6609).  Provides a bridge from AVRCP/MPRIS speaker button
commands to MPD playback control via python-mpd2.

HA's built-in MPD integration connects to each port to create a
media_player entity per speaker.
"""

import asyncio
import logging
import os
import textwrap

from mpd.asyncio import MPDClient

logger = logging.getLogger(__name__)

MPD_HOST = "127.0.0.1"


class MPDManager:
    """Manages an embedded MPD daemon and bridges AVRCP commands to it."""

    _version_logged = False

    def __init__(
        self,
        address: str,
        port: int,
        speaker_name: str,
        password: str | None = None,
        log_level: str = "info",
    ) -> None:
        self._address = address
        self._port = port
        self._speaker_name = speaker_name
        self._password = password
        # Map app log level to MPD log level:
        # debug → "verbose" (full client/command chatter)
        # anything else → "default" (errors/warnings only)
        self._mpd_log_level = "verbose" if log_level == "debug" else "default"

        # Ephemeral per-instance dir for config, pid, and state.
        self._tmp_dir = f"/tmp/mpd_{port}"
        self._conf_path = f"{self._tmp_dir}/mpd.conf"
        self._pid_file = f"{self._tmp_dir}/pid"

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
        os.makedirs(f"{self._tmp_dir}/playlists", exist_ok=True)
        self._generate_config()
        await self._start_daemon()
        await self._connect_client()
        self._running = True
        logger.info("MPD started for %s on port %d", self._address, self._port)

    async def stop(self) -> None:
        """Disconnect client and terminate MPD daemon."""
        self._running = False

        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None

        self._disconnect_client()

        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
            logger.info("MPD daemon stopped (port %d)", self._port)
        self._process = None

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None

    @property
    def port(self) -> int:
        return self._port

    @property
    def address(self) -> str:
        return self._address

    # -- Config generation --

    def _generate_config(self) -> None:
        """Write a minimal mpd.conf targeting a specific PulseAudio sink."""
        password_line = ""
        if self._password:
            password_line = f'password "{self._password}@read,add,control,admin"'

        config = textwrap.dedent("""\
            playlist_directory  "{tmp_dir}/playlists"
            state_file          "{tmp_dir}/state"
            pid_file            "{pid_file}"
            bind_to_address     "0.0.0.0"
            port                "{port}"
            log_level           "{mpd_log_level}"
            {password_line}

            audio_output {{
                type    "pulse"
                name    "{speaker_name}"
                sink    "{sink}"
            }}

            input {{
                plugin  "curl"
            }}
        """).format(
            tmp_dir=self._tmp_dir,
            pid_file=self._pid_file,
            port=self._port,
            password_line=password_line,
            speaker_name=self._speaker_name.replace("\\", "\\\\").replace('"', '\\"'),
            sink=self._sink_name.replace("\\", "\\\\").replace('"', '\\"'),
            mpd_log_level=self._mpd_log_level,
        )

        with open(self._conf_path, "w") as f:
            f.write(config)
        logger.debug("MPD config written to %s", self._conf_path)

    # -- Daemon management --

    async def _log_mpd_version(self) -> None:
        """Log the installed MPD version on first use."""
        if MPDManager._version_logged:
            return
        MPDManager._version_logged = True
        try:
            proc = await asyncio.create_subprocess_exec(
                "mpd", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            first_line = stdout.decode().split("\n", 1)[0].strip()
            if first_line:
                logger.info("MPD version: %s", first_line)
        except Exception as e:
            logger.debug("Could not determine MPD version: %s", e)

    async def _start_daemon(self) -> None:
        """Start MPD in foreground mode as a subprocess."""
        await self._log_mpd_version()
        self._process = await asyncio.create_subprocess_exec(
            "mpd", "--no-daemon", "--stderr", self._conf_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        # Give MPD a moment to initialize
        await asyncio.sleep(0.5)
        if self._process.returncode is not None:
            stderr = await self._process.stderr.read()
            raise RuntimeError(f"MPD failed to start: {stderr.decode().strip()}")
        logger.info("MPD daemon started (pid=%d, port=%d)", self._process.pid, self._port)
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
                    logger.info("[mpd:%d] %s", self._port, text)
        except Exception:
            pass

    # -- Client connection --

    def _disconnect_client(self) -> None:
        """Disconnect and clean up the python-mpd2 client.

        Must be called before dropping the reference to avoid orphaning
        the internal ``__run_task`` (causes 'Task was destroyed but it
        is pending!' errors).
        """
        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    @staticmethod
    def _is_connection_error(exc: Exception) -> bool:
        """Return True if the exception indicates a broken connection."""
        return isinstance(exc, (ConnectionError, BrokenPipeError, OSError, EOFError))

    async def _connect_client(self) -> None:
        """Connect the python-mpd2 async client."""
        self._client = MPDClient()
        for attempt in range(5):
            try:
                await self._client.connect(MPD_HOST, self._port)
                if self._password:
                    await self._client.password(self._password)
                logger.debug("MPD client connected (port %d)", self._port)
                return
            except (ConnectionRefusedError, OSError):
                if attempt < 4:
                    await asyncio.sleep(0.5)
        logger.warning("Could not connect MPD client after retries (port %d)", self._port)
        self._client = None

    async def _ensure_connected(self) -> None:
        """Reconnect client if the connection was lost."""
        if self._client:
            try:
                await self._client.ping()
                return
            except Exception:
                self._disconnect_client()

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
            logger.warning("MPD command %s failed (port %d): %s", command, self._port, e)
            if self._is_connection_error(e):
                self._disconnect_client()

    async def set_volume(self, vol_pct: int) -> None:
        """Set MPD volume (0-100)."""
        await self._ensure_connected()
        if not self._client:
            return
        try:
            await self._client.setvol(max(0, min(100, vol_pct)))
        except Exception as e:
            logger.debug("MPD set_volume failed (port %d): %s", self._port, e)
            if self._is_connection_error(e):
                self._disconnect_client()

    async def get_status(self) -> dict:
        """Return MPD status dict."""
        await self._ensure_connected()
        if not self._client:
            return {"state": "unknown"}
        try:
            return await self._client.status()
        except Exception as e:
            if self._is_connection_error(e):
                self._disconnect_client()
            return {"state": "unknown"}
