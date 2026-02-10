"""BlueZ Agent1 D-Bus implementation for automated audio device pairing."""

import logging

from dbus_next import BusType
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, method

from .constants import (
    AGENT_CAPABILITY,
    AGENT_MANAGER_INTERFACE,
    AGENT_PATH,
    BLUEZ_SERVICE,
)

logger = logging.getLogger(__name__)


class AgentInterface(ServiceInterface):
    """D-Bus implementation of org.bluez.Agent1.

    Uses NoInputNoOutput capability for Just Works pairing, which is
    appropriate for Bluetooth speakers and audio receivers that don't
    have displays or keyboards.
    """

    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self) -> None:
        """Called when BlueZ unregisters this agent."""
        logger.debug("Agent released by BlueZ")

    @method()
    def RequestAuthorization(self, device: "o") -> None:
        """Auto-authorize incoming pairing requests."""
        logger.info("Auto-authorizing pairing for %s", device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:
        """Auto-authorize service connections (A2DP, AVRCP, etc.)."""
        logger.info("Auto-authorizing service %s for %s", uuid, device)

    @method()
    def Cancel(self) -> None:
        """Pairing request was cancelled."""
        logger.debug("Agent: pairing cancelled")


class PairingAgent:
    """Manages the lifecycle of a BlueZ pairing agent.

    Registers an Agent1 implementation on D-Bus and sets it as the
    default agent for handling pairing requests.
    """

    def __init__(self, bus: MessageBus):
        self._bus = bus
        self._agent = AgentInterface()
        self._registered = False

    async def register(self) -> None:
        """Export the agent interface and register with BlueZ."""
        self._bus.export(AGENT_PATH, self._agent)

        introspection = await self._bus.introspect(BLUEZ_SERVICE, "/org/bluez")
        proxy = self._bus.get_proxy_object(
            BLUEZ_SERVICE, "/org/bluez", introspection
        )
        agent_manager = proxy.get_interface(AGENT_MANAGER_INTERFACE)

        await agent_manager.call_register_agent(AGENT_PATH, AGENT_CAPABILITY)
        await agent_manager.call_request_default_agent(AGENT_PATH)
        self._registered = True
        logger.info(
            "Pairing agent registered at %s (capability: %s)",
            AGENT_PATH,
            AGENT_CAPABILITY,
        )

    async def unregister(self) -> None:
        """Unregister the agent from BlueZ."""
        if not self._registered:
            return
        try:
            introspection = await self._bus.introspect(BLUEZ_SERVICE, "/org/bluez")
            proxy = self._bus.get_proxy_object(
                BLUEZ_SERVICE, "/org/bluez", introspection
            )
            agent_manager = proxy.get_interface(AGENT_MANAGER_INTERFACE)
            await agent_manager.call_unregister_agent(AGENT_PATH)
        except DBusError as e:
            logger.debug("Agent unregister failed (may already be gone): %s", e)
        finally:
            self._bus.unexport(AGENT_PATH, self._agent)
            self._registered = False
        logger.info("Pairing agent unregistered")
