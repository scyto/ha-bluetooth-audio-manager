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
        # Repeat mode + guard against a reconnect loop (see enable_repeat)
        self._stream_ends: list[float] = []
        self._repeat_guard_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None  # see _watch_playback

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
        self._watch_task = asyncio.create_task(self._watch_playback())
        logger.info("MPD started for %s on port %d", self._address, self._port)

    async def stop(self) -> None:
        """Disconnect client and terminate MPD daemon."""
        self._running = False
        for task in (self._repeat_guard_task, self._watch_task):
            if task and not task.done():
                task.cancel()
        self._repeat_guard_task = None
        self._watch_task = None

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
        """Whether the daemon is up *right now*.

        The ``returncode`` check matters: MPD can exit on its own (its PA
        sink disappearing during a Bluetooth drop is the common case).  A
        holder that only asked "do I have an instance?" would then treat a
        dead daemon as live and never restart it, leaving the media_player
        entity permanently unavailable with nothing in the log.
        """
        return (
            self._running
            and self._process is not None
            and self._process.returncode is None
        )

    @property
    def pid(self) -> int | None:
        """PID of the running daemon (None when not running)."""
        return self._process.pid if self.is_running else None

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
            pid_file            "{pid_file}"
            bind_to_address     "0.0.0.0"
            port                "{port}"
            log_level           "{mpd_log_level}"
            {password_line}

            audio_output {{
                type    "pulse"
                name    "{speaker_name}"
                sink    "{sink}"
                # software mixer is always "online", so MPD always reports a
                # "volume" field — even when stopped. The pulse mixer (MPD's
                # default for this plugin) only reports volume while a
                # sink-input exists (i.e. during playback), so HA's MPD
                # integration would hide the volume slider whenever idle. (#274)
                mixer_type    "software"
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

    def _daemon_env(self) -> dict[str, str]:
        """Build the environment for the MPD subprocess.

        Gives each MPD instance a private PulseAudio stream-restore entry.

        MPD's pulse output plugin sets ``media.role=music`` on its stream
        (it does its own ``setenv("PULSE_PROP_media.role", "music")``).
        PulseAudio's ``module-stream-restore`` derives the key for its
        saved per-stream volume from the proplist in a fixed order —
        ``module-stream-restore.id``, then ``media.role``, then
        application id/name (``pa_proplist_get_stream_group()``).  Role
        wins, so every MPD instance would share the single
        ``sink-input-by-media-role:music`` entry with every other
        ``media.role=music`` client on HAOS's shared PulseAudio server:
        HA's own players, Music Assistant, other add-ons.  Whatever
        volume one of them last saved is then applied to our sink-input
        at creation — invisible to both MPD and HA, because MPD's
        software mixer is a separate layer from the PA stream volume.

        Setting ``module-stream-restore.id`` per speaker takes precedence
        over the role and isolates us from that shared entry.  libpulse
        reads ``PULSE_PROP_<key>`` verbatim at context creation (key up
        to the first '=', value is the rest, no unescaping), so the
        hyphens in the key and the colons in the MAC are both safe.
        (#285)
        """
        env = dict(os.environ)
        env["PULSE_PROP_module-stream-restore.id"] = f"bt-audio-manager:{self._address}"
        return env

    async def _start_daemon(self) -> None:
        """Start MPD in foreground mode as a subprocess."""
        await self._log_mpd_version()
        self._process = await asyncio.create_subprocess_exec(
            "mpd", "--no-daemon", "--stderr", self._conf_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=self._daemon_env(),
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
                    if "player: played " in text:
                        self._on_stream_end()
        except Exception:
            pass

    # -- Repeat mode ---------------------------------------------------------
    #
    # A live radio stream "ends" whenever the server closes the connection.
    # MPD then logs "player: played <url>" and simply stops: the speaker
    # stays silent until something outside notices and plays again (minutes,
    # with a polling watchdog).  With repeat on and a single-entry queue MPD
    # re-opens the same URL straight away.  A real error (server down) still
    # stops playback, so repeat alone cannot spin.  The guard below covers
    # the remaining case of a server that keeps accepting and immediately
    # closing: too many ends in a short time switch repeat off for a while.

    REPEAT_GUARD_MAX_ENDS = 5      # stream ends tolerated within the window
    REPEAT_GUARD_WINDOW = 60.0     # seconds
    REPEAT_GUARD_COOLDOWN = 600.0  # seconds repeat stays off after tripping

    async def enable_repeat(self) -> bool:
        """Turn MPD's repeat mode on (lost whenever the daemon restarts)."""
        await self._ensure_connected()
        if not self._client:
            return False
        try:
            await self._client.repeat(1)
            await self._client.single(0)
            return True
        except Exception as e:
            logger.debug("MPD repeat on failed (port %d): %s", self._port, e)
            if self._is_connection_error(e):
                self._disconnect_client()
            return False

    async def _set_repeat_off(self) -> None:
        await self._ensure_connected()
        if not self._client:
            return
        try:
            await self._client.repeat(0)
        except Exception as e:
            logger.debug("MPD repeat off failed (port %d): %s", self._port, e)
            if self._is_connection_error(e):
                self._disconnect_client()

    def _on_stream_end(self) -> None:
        now = asyncio.get_running_loop().time()
        self._stream_ends = [t for t in self._stream_ends if now - t < self.REPEAT_GUARD_WINDOW]
        self._stream_ends.append(now)
        if len(self._stream_ends) <= self.REPEAT_GUARD_MAX_ENDS:
            return
        if self._repeat_guard_task and not self._repeat_guard_task.done():
            return  # already tripped
        logger.warning(
            "[mpd:%d] stream ended %d times within %ds — repeat off for %ds "
            "to avoid hammering the server",
            self._port, len(self._stream_ends), int(self.REPEAT_GUARD_WINDOW),
            int(self.REPEAT_GUARD_COOLDOWN),
        )
        self._stream_ends.clear()
        self._repeat_guard_task = asyncio.ensure_future(self._repeat_cooldown())

    async def _repeat_cooldown(self) -> None:
        await self._set_repeat_off()
        await asyncio.sleep(self.REPEAT_GUARD_COOLDOWN)
        if self.is_running and await self.enable_repeat():
            logger.info("[mpd:%d] repeat back on after cooldown", self._port)

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

    async def restart_playback(self) -> bool:
        """Stop and start playback, re-opening the stream and the audio output.

        Used after a Bluetooth discovery scan, which can stall the A2DP link
        long enough that MPD's output never recovers on its own (#319).
        Equivalent to what a fresh ``media_player.play_media`` achieves.
        """
        await self._ensure_connected()
        if not self._client:
            return False
        try:
            await self._client.stop()
            await asyncio.sleep(0.5)
            await self._client.play()
            return True
        except Exception as e:
            logger.warning("MPD restart failed (port %d): %s", self._port, e)
            if self._is_connection_error(e):
                self._disconnect_client()
            return False

    # -- Retry after a stream error -------------------------------------------
    #
    # When the stream cannot be opened — internet down, DNS failing, the
    # station offline — MPD stops with an error and stays stopped; repeat
    # does not help because MPD deliberately stops on errors.  Keep trying
    # with backoff until it plays again.  Only an *error* stop is retried:
    # MPD reports it in status["error"], a plain stop or pause has none.

    PLAY_RETRY_DELAYS = (5, 10, 20, 60)  # seconds; the last one repeats
    WATCH_INTERVAL = 5.0  # seconds between status checks

    async def _watch_playback(self) -> None:
        loop = asyncio.get_running_loop()
        attempt = 0
        next_try: float | None = None
        try:
            while self._running:
                await asyncio.sleep(self.WATCH_INTERVAL)
                status = await self.get_status()
                state = status.get("state")
                if state == "play":
                    if attempt:
                        logger.info(
                            "[mpd:%d] playback recovered after %d retr%s",
                            self._port, attempt, "y" if attempt == 1 else "ies",
                        )
                    attempt, next_try = 0, None
                    continue
                error = status.get("error")
                if state != "stop" or not error:
                    attempt, next_try = 0, None  # paused, stopped on purpose, or unknown
                    continue
                now = loop.time()
                if next_try is None:
                    delay = self.PLAY_RETRY_DELAYS[min(attempt, len(self.PLAY_RETRY_DELAYS) - 1)]
                    next_try = now + delay
                    log = logger.warning if attempt == 0 else logger.info
                    log(
                        "[mpd:%d] playback stopped with error (%s) — retrying in %ds",
                        self._port, error, delay,
                    )
                    continue
                if now < next_try:
                    continue
                attempt += 1
                next_try = None
                await self._retry_play()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[mpd:%d] playback watch ended: %s", self._port, e)

    async def _retry_play(self) -> None:
        await self._ensure_connected()
        if not self._client:
            return
        try:
            await self._client.clearerror()
            await self._client.play()
        except Exception as e:
            logger.debug("MPD retry play failed (port %d): %s", self._port, e)
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
