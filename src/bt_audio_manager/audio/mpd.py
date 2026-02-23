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
import pwd
import textwrap

from mpd.asyncio import MPDClient

logger = logging.getLogger(__name__)

MPD_BASE_DIR = "/data/mpd"
MPD_MUSIC_DIR = "/data/mpd/music"
MPD_PLAYLIST_DIR = "/data/mpd/playlists"
MPD_HOST = "127.0.0.1"


def _chown_mpd_dirs() -> None:
    """Recursively chown MPD data dirs to the 'mpd' user/group.

    Alpine's mpd package creates the mpd user.  MPD drops privileges from
    root to this user at startup, so it must own its data directories.
    """
    try:
        pw = pwd.getpwnam("mpd")
        for dirpath, dirnames, filenames in os.walk(MPD_BASE_DIR):
            os.chown(dirpath, pw.pw_uid, pw.pw_gid)
            for fname in filenames:
                os.chown(os.path.join(dirpath, fname), pw.pw_uid, pw.pw_gid)
        logger.debug("chown'd %s to mpd:%d", MPD_BASE_DIR, pw.pw_uid)
    except KeyError:
        logger.warning("'mpd' user not found — MPD may fail to write its database")
    except OSError as e:
        logger.warning("Failed to chown MPD dirs: %s", e)


class MPDManager:
    """Manages an embedded MPD daemon and bridges AVRCP commands to it."""

    def __init__(
        self,
        address: str,
        port: int,
        speaker_name: str,
        password: str | None = None,
    ) -> None:
        self._address = address
        self._port = port
        self._speaker_name = speaker_name
        self._password = password

        # Per-instance paths (port as discriminator)
        self._instance_dir = f"{MPD_BASE_DIR}/instance_{port}"
        self._db_file = f"{self._instance_dir}/database"
        self._state_file = f"{self._instance_dir}/state"
        self._conf_path = f"/tmp/mpd_{port}.conf"
        self._pid_file = f"/tmp/mpd_{port}.pid"

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
        os.makedirs(MPD_PLAYLIST_DIR, exist_ok=True)
        os.makedirs(self._instance_dir, exist_ok=True)
        # MPD drops from root to the 'mpd' user; ensure it owns its data dirs
        _chown_mpd_dirs()
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
            music_directory     "{music_dir}"
            playlist_directory  "{playlist_dir}"
            db_file             "{db_file}"
            state_file          "{state_file}"
            pid_file            "{pid_file}"
            bind_to_address     "0.0.0.0"
            port                "{port}"
            log_level           "verbose"
            auto_update         "no"
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
            music_dir=MPD_MUSIC_DIR,
            playlist_dir=MPD_PLAYLIST_DIR,
            db_file=self._db_file,
            state_file=self._state_file,
            pid_file=self._pid_file,
            port=self._port,
            password_line=password_line,
            speaker_name=self._speaker_name.replace("\\", "\\\\").replace('"', '\\"'),
            sink=self._sink_name.replace("\\", "\\\\").replace('"', '\\"'),
        )

        with open(self._conf_path, "w") as f:
            f.write(config)
        logger.debug("MPD config written to %s", self._conf_path)

    # -- Daemon management --

    async def _start_daemon(self) -> None:
        """Start MPD in foreground mode as a subprocess."""
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
