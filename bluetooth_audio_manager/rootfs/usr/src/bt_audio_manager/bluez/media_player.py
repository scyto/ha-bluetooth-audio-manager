"""MPRIS MediaPlayer2.Player implementation for receiving AVRCP commands.

When a Bluetooth speaker (AVRCP Controller) sends play/pause/skip/volume
commands, BlueZ forwards them as D-Bus method calls to a registered MPRIS
player.  This module exports that player and registers it with BlueZ via
org.bluez.Media1.RegisterPlayer().

The speaker buttons then appear as events in the add-on's UI.
"""

import logging
from typing import Callable

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, method, dbus_property, signal

from .constants import BLUEZ_SERVICE, DEFAULT_ADAPTER_PATH, MEDIA_INTERFACE, PLAYER_PATH

logger = logging.getLogger(__name__)


class MPRISPlayerInterface(ServiceInterface):
    """D-Bus implementation of org.mpris.MediaPlayer2.Player.

    BlueZ calls these methods directly when it receives AVRCP passthrough
    commands from a connected Bluetooth speaker/headset.
    """

    def __init__(self, command_callback: Callable[[str], None]):
        super().__init__("org.mpris.MediaPlayer2.Player")
        self._callback = command_callback
        self._playback_status = "Stopped"
        self._volume = 1.0

    # -- AVRCP command handlers (BlueZ calls these) --

    @method()
    def Play(self) -> None:
        logger.info("AVRCP command: Play")
        self._playback_status = "Playing"
        self.emit_properties_changed({"PlaybackStatus": self._playback_status})
        self._callback("Play")

    @method()
    def Pause(self) -> None:
        logger.info("AVRCP command: Pause")
        self._playback_status = "Paused"
        self.emit_properties_changed({"PlaybackStatus": self._playback_status})
        self._callback("Pause")

    @method()
    def PlayPause(self) -> None:
        logger.info("AVRCP command: PlayPause")
        if self._playback_status == "Playing":
            self._playback_status = "Paused"
        else:
            self._playback_status = "Playing"
        self.emit_properties_changed({"PlaybackStatus": self._playback_status})
        self._callback("PlayPause")

    @method()
    def Stop(self) -> None:
        logger.info("AVRCP command: Stop")
        self._playback_status = "Stopped"
        self.emit_properties_changed({"PlaybackStatus": self._playback_status})
        self._callback("Stop")

    @method()
    def Next(self) -> None:
        logger.info("AVRCP command: Next")
        self._callback("Next")

    @method()
    def Previous(self) -> None:
        logger.info("AVRCP command: Previous")
        self._callback("Previous")

    @method()
    def Seek(self, offset: "x") -> None:
        logger.debug("AVRCP command: Seek offset=%d", offset)
        self._callback("Seek")

    @method()
    def SetPosition(self, track_id: "o", position: "x") -> None:
        logger.debug("AVRCP command: SetPosition pos=%d", position)

    @method()
    def OpenUri(self, uri: "s") -> None:
        logger.debug("AVRCP command: OpenUri uri=%s", uri)

    # -- Required MPRIS properties --

    @dbus_property()
    def PlaybackStatus(self) -> "s":
        return self._playback_status

    @dbus_property()
    def LoopStatus(self) -> "s":
        return "None"

    @LoopStatus.setter
    def LoopStatus(self, val: "s"):
        pass  # read-only for our purposes

    @dbus_property()
    def Rate(self) -> "d":
        return 1.0

    @Rate.setter
    def Rate(self, val: "d"):
        pass

    @dbus_property()
    def Shuffle(self) -> "b":
        return False

    @Shuffle.setter
    def Shuffle(self, val: "b"):
        pass

    @dbus_property()
    def Metadata(self) -> "a{sv}":
        return {
            "xesam:title": Variant("s", "Home Assistant Audio"),
            "xesam:artist": Variant("as", [""]),
            "mpris:length": Variant("x", 0),
        }

    @dbus_property()
    def Volume(self) -> "d":
        return self._volume

    @Volume.setter
    def Volume(self, val: "d"):
        old = self._volume
        self._volume = max(0.0, min(1.0, val))
        if abs(old - self._volume) > 0.01:
            logger.info("AVRCP volume: %.0f%%", self._volume * 100)
            self._callback("Volume")

    @dbus_property()
    def Position(self) -> "x":
        return 0

    @dbus_property()
    def MinimumRate(self) -> "d":
        return 1.0

    @dbus_property()
    def MaximumRate(self) -> "d":
        return 1.0

    @dbus_property()
    def CanGoNext(self) -> "b":
        return True

    @dbus_property()
    def CanGoPrevious(self) -> "b":
        return True

    @dbus_property()
    def CanPlay(self) -> "b":
        return True

    @dbus_property()
    def CanPause(self) -> "b":
        return True

    @dbus_property()
    def CanSeek(self) -> "b":
        return False

    @dbus_property()
    def CanControl(self) -> "b":
        return True

    @signal()
    def Seeked(self) -> "x":
        return 0


class AVRCPMediaPlayer:
    """Manages the lifecycle of an MPRIS player registered with BlueZ.

    Follows the same pattern as PairingAgent: export a D-Bus interface,
    then register it with BlueZ.
    """

    def __init__(self, bus: MessageBus, command_callback: Callable[[str], None]):
        self._bus = bus
        self._player = MPRISPlayerInterface(command_callback)
        self._registered = False

    async def register(self) -> None:
        """Export the player interface and register with BlueZ Media1."""
        self._bus.export(PLAYER_PATH, self._player)

        properties = {
            "PlaybackStatus": Variant("s", "Stopped"),
            "LoopStatus": Variant("s", "None"),
            "Rate": Variant("d", 1.0),
            "Shuffle": Variant("b", False),
            "Metadata": Variant("a{sv}", {
                "xesam:title": Variant("s", "Home Assistant Audio"),
                "xesam:artist": Variant("as", [""]),
                "mpris:length": Variant("x", 0),
            }),
            "Volume": Variant("d", 1.0),
            "Position": Variant("x", 0),
            "MinimumRate": Variant("d", 1.0),
            "MaximumRate": Variant("d", 1.0),
            "CanGoNext": Variant("b", True),
            "CanGoPrevious": Variant("b", True),
            "CanPlay": Variant("b", True),
            "CanPause": Variant("b", True),
            "CanSeek": Variant("b", False),
            "CanControl": Variant("b", True),
        }

        introspection = await self._bus.introspect(BLUEZ_SERVICE, DEFAULT_ADAPTER_PATH)
        proxy = self._bus.get_proxy_object(
            BLUEZ_SERVICE, DEFAULT_ADAPTER_PATH, introspection
        )
        media = proxy.get_interface(MEDIA_INTERFACE)

        await media.call_register_player(PLAYER_PATH, properties)
        self._registered = True
        logger.info(
            "AVRCP media player registered at %s (receives speaker button events)",
            PLAYER_PATH,
        )

    async def unregister(self) -> None:
        """Unregister the player from BlueZ."""
        if not self._registered:
            return
        try:
            introspection = await self._bus.introspect(BLUEZ_SERVICE, DEFAULT_ADAPTER_PATH)
            proxy = self._bus.get_proxy_object(
                BLUEZ_SERVICE, DEFAULT_ADAPTER_PATH, introspection
            )
            media = proxy.get_interface(MEDIA_INTERFACE)
            await media.call_unregister_player(PLAYER_PATH)
        except DBusError as e:
            logger.debug("Player unregister failed (may already be gone): %s", e)
        finally:
            self._bus.unexport(PLAYER_PATH, self._player)
            self._registered = False
        logger.info("AVRCP media player unregistered")
