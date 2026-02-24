"""BlueZ D-Bus pairing agent for PIN-based OneControl gateways.

PIN-based (legacy) gateways require a numeric passkey during BLE bonding.
On HAOS (Linux/BlueZ), we register a temporary D-Bus ``org.bluez.Agent1``
that provides the passkey when BlueZ requests it during ``Device1.Pair()``.

This mirrors the Android flow:
  1. connectGatt()
  2. createBond()
  3. BroadcastReceiver intercepts ACTION_PAIRING_REQUEST
  4. device.setPin(pin) + abortBroadcast()

Limitations:
  - Only works on Linux with BlueZ (i.e. HAOS, not macOS dev machines).
  - NOT supported via ESPHome Bluetooth Proxy (proxy doesn't forward
    passkey requests — requires a direct USB Bluetooth adapter on the
    HA host).

Reference: Android BaseBleService.kt § pairingRequestReceiver,
           OneControlDevicePlugin.kt § getBondingPin()

NOTE: This file intentionally does NOT use ``from __future__ import annotations``
because dbus_fast's ``@method()`` decorator inspects annotation strings
at class-definition time.  PEP 563 would double-quote the D-Bus type
characters (e.g. ``'o'`` → ``"'o'"``) and break ``parse_annotation()``.
"""

import asyncio
import logging
import platform
from typing import Any

_LOGGER = logging.getLogger(__name__)

AGENT_PATH = "/org/homeassistant/onecontrol/pin_agent"
BLUEZ_SERVICE = "org.bluez"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
DEVICE_IFACE = "org.bluez.Device1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

# ---------------------------------------------------------------------------
# Platform detection — D-Bus only available on Linux (HAOS)
# ---------------------------------------------------------------------------
_DBUS_AVAILABLE = False
if platform.system() == "Linux":
    try:
        from dbus_fast import BusType, Message, MessageType  # noqa: F401
        from dbus_fast.aio import MessageBus  # noqa: F401
        from dbus_fast.service import ServiceInterface, method  # noqa: F401

        _DBUS_AVAILABLE = True
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# D-Bus Agent1 interface (only defined on Linux)
# ---------------------------------------------------------------------------
if _DBUS_AVAILABLE:

    class _PinAgentInterface(ServiceInterface):  # type: ignore[misc]
        """org.bluez.Agent1 implementation that provides a passkey/PIN."""

        def __init__(self, passkey: int, pin_code: str) -> None:
            super().__init__("org.bluez.Agent1")
            self._passkey = passkey
            self._pin_code = pin_code
            self._responded = False

        @property
        def responded(self) -> bool:
            """True if BlueZ actually asked us for the passkey."""
            return self._responded

        # -- Agent1 methods ------------------------------------------------

        @method()  # type: ignore[misc]
        def Release(self) -> None:  # noqa: N802
            """Called when BlueZ unregisters the agent."""
            _LOGGER.debug("PIN Agent: Release")

        @method()  # type: ignore[misc]
        def RequestPinCode(self, device: "o") -> "s":  # type: ignore[name-defined]  # noqa: N802, F821
            """Legacy PIN code request (string)."""
            _LOGGER.info(
                "PIN Agent: RequestPinCode for %s — providing %d-char string PIN "
                "(BlueZ chose STRING pin path)",
                device, len(self._pin_code),
            )
            self._responded = True
            return self._pin_code

        @method()  # type: ignore[misc]
        def RequestPasskey(self, device: "o") -> "u":  # type: ignore[name-defined]  # noqa: N802, F821
            """Numeric passkey request (uint32)."""
            _LOGGER.info(
                "PIN Agent: RequestPasskey for %s — providing uint32=%d "
                "(BlueZ chose NUMERIC passkey path)",
                device, self._passkey,
            )
            self._responded = True
            return self._passkey

        @method()  # type: ignore[misc]
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:  # type: ignore[name-defined]  # noqa: N802, F821
            """Display passkey to user (we just log it)."""
            _LOGGER.info(
                "PIN Agent: DisplayPasskey %d (entered=%d) for %s "
                "(BlueZ display-only — no response needed)",
                passkey, entered, device,
            )

        @method()  # type: ignore[misc]
        def DisplayPinCode(self, device: "o", pincode: "s") -> None:  # type: ignore[name-defined]  # noqa: N802, F821
            """Display PIN code to user."""
            _LOGGER.info(
                "PIN Agent: DisplayPinCode '%s' for %s "
                "(BlueZ display-only — no response needed)",
                pincode, device,
            )

        @method()  # type: ignore[misc]
        def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # type: ignore[name-defined]  # noqa: N802, F821
            """Confirm passkey — auto-accept."""
            _LOGGER.info(
                "PIN Agent: RequestConfirmation passkey=%d for %s — auto-accepting "
                "(Just Works / numeric comparison)",
                passkey, device,
            )

        @method()  # type: ignore[misc]
        def RequestAuthorization(self, device: "o") -> None:  # type: ignore[name-defined]  # noqa: N802, F821
            """Authorize device — auto-accept."""
            _LOGGER.info("PIN Agent: RequestAuthorization for %s — auto-accepting", device)

        @method()  # type: ignore[misc]
        def AuthorizeService(self, device: "o", uuid: "s") -> None:  # type: ignore[name-defined]  # noqa: N802, F821
            """Authorize service — auto-accept."""
            _LOGGER.info("PIN Agent: AuthorizeService %s on %s — auto-accepting", uuid, device)

        @method()  # type: ignore[misc]
        def Cancel(self) -> None:  # noqa: N802
            """Pairing was cancelled."""
            _LOGGER.warning(
                "PIN Agent: Cancel — pairing cancelled by BlueZ "
                "(gateway may have rejected credentials or timed out)"
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_pin_pairing_supported() -> bool:
    """Return True if this platform can do D-Bus PIN pairing."""
    return _DBUS_AVAILABLE


class PinAgentContext:
    """Holds a registered D-Bus PIN agent that is waiting for a pair() call.

    Created by prepare_pin_agent().  The caller must connect GATT and then
    call client.pair() while this context is active — BlueZ will invoke our
    agent's RequestPinCode/RequestPasskey method during that pair() call.

    Must always be cleaned up via cleanup(), even on failure.
    """

    def __init__(
        self,
        bus: Any,
        agent_registered: bool,
        already_bonded: bool = False,
        agent: Any = None,
    ) -> None:
        self.bus = bus
        self.agent_registered = agent_registered
        self.already_bonded = already_bonded
        self._agent = agent

    @property
    def agent_responded(self) -> bool:
        """True if BlueZ actually called our agent for the PIN."""
        return bool(self._agent and self._agent.responded)

    async def cleanup(self) -> None:
        """Unregister agent and disconnect D-Bus."""
        if self.agent_registered and self.bus:
            try:
                from dbus_fast import Message  # noqa: F811

                await self.bus.call(
                    Message(
                        destination=BLUEZ_SERVICE,
                        path="/org/bluez",
                        interface=AGENT_MANAGER_IFACE,
                        member="UnregisterAgent",
                        signature="o",
                        body=[AGENT_PATH],
                    )
                )
                _LOGGER.debug("PIN agent unregistered")
            except Exception as exc:
                _LOGGER.debug("Agent cleanup error: %s", exc)
        if self.bus:
            self.bus.disconnect()
            self.bus = None


async def prepare_pin_agent(
    device_address: str,
    pin: str,
) -> PinAgentContext | None:
    """Register a D-Bus PIN agent WITHOUT calling Device1.Pair().

    This follows the Android pattern: register the agent so it is ready,
    then connect GATT, then call client.pair() — BlueZ will invoke our agent
    during that pair() call to provide the PIN.

    Returns a PinAgentContext that MUST be cleaned up via ctx.cleanup().
    Returns None if D-Bus is not available on this platform.
    If the device is already bonded, ctx.already_bonded is True and no
    agent is registered (cleanup() is still safe to call).
    """
    if not _DBUS_AVAILABLE:
        return None

    from dbus_fast import BusType  # noqa: F811
    from dbus_fast.aio import MessageBus  # noqa: F811

    passkey = int(pin) if pin.isdigit() else 0

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        _LOGGER.error("Cannot connect to system D-Bus: %s", exc)
        return None

    try:
        # If already bonded, no agent registration needed.
        device_path = await _find_device_path(bus, device_address)
        if device_path and await _is_paired(bus, device_path):
            _LOGGER.info(
                "Device %s already bonded in BlueZ — PIN agent not needed",
                device_address,
            )
            return PinAgentContext(bus=bus, agent_registered=False, already_bonded=True)

        # Register PIN agent and leave it active for the upcoming pair() call.
        agent = _PinAgentInterface(passkey, pin)
        bus.export(AGENT_PATH, agent)

        agent_registered = await _register_agent(bus)
        if not agent_registered:
            _LOGGER.error("Failed to register PIN agent with BlueZ")
            bus.disconnect()
            return None

        _LOGGER.info(
            "PIN agent registered for %s (passkey=%d) — "
            "connect GATT then call pair() to complete bonding",
            device_address,
            passkey,
        )
        return PinAgentContext(
            bus=bus,
            agent_registered=True,
            already_bonded=False,
            agent=agent,
        )

    except Exception as exc:
        _LOGGER.error("prepare_pin_agent failed for %s: %s", device_address, exc)
        try:
            bus.disconnect()
        except Exception:
            pass
        return None


async def pair_with_pin(
    device_address: str,
    pin: str,
    timeout: float = 30.0,
) -> bool:
    """Register a temporary D-Bus agent, pair via BlueZ, clean up.

    Args:
        device_address: BLE MAC address (e.g. "AA:BB:CC:DD:EE:FF").
        pin: The 6-digit PIN string from the gateway sticker.
        timeout: Seconds to wait for pairing to complete.

    Returns:
        True if pairing succeeded or device was already bonded.
        False on failure (with details logged).
    """
    if not _DBUS_AVAILABLE:
        _LOGGER.error(
            "PIN pairing requires Linux/HAOS with BlueZ and dbus-fast — "
            "not available on %s",
            platform.system(),
        )
        return False

    from dbus_fast import BusType, Message, MessageType  # noqa: F811
    from dbus_fast.aio import MessageBus  # noqa: F811

    passkey = int(pin) if pin.isdigit() else 0
    bus: Any = None
    agent_registered = False

    _LOGGER.info(
        "PIN pairing setup: address=%s, pin_length=%d, pin_is_numeric=%s, "
        "passkey_uint32=%d, capability=KeyboardDisplay",
        device_address, len(pin), pin.isdigit(), passkey,
    )

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        _LOGGER.error("Cannot connect to system D-Bus: %s", exc)
        return False

    try:
        # ── Find device in BlueZ object tree ──────────────────────────
        device_path = await _find_device_path(bus, device_address)
        if not device_path:
            _LOGGER.warning(
                "Device %s not found in BlueZ object tree — D-Bus PIN pairing "
                "unavailable. Will fall back to Bleak pair() after connect "
                "(may work via ESPHome Bluetooth Proxy).",
                device_address,
            )
            return False

        # ── Check if already bonded ───────────────────────────────────
        if await _is_paired(bus, device_path):
            _LOGGER.info(
                "Device %s already bonded in BlueZ — skipping PIN pairing",
                device_address,
            )
            return True

        # ── Register our PIN agent ────────────────────────────────────
        agent = _PinAgentInterface(passkey, pin)
        bus.export(AGENT_PATH, agent)

        agent_registered = await _register_agent(bus)
        if not agent_registered:
            _LOGGER.error("Failed to register PIN agent with BlueZ")
            return False

        _LOGGER.info(
            "PIN agent registered — initiating pairing with %s (passkey=%d)",
            device_address,
            passkey,
        )

        # ── Call Device1.Pair() ───────────────────────────────────────
        try:
            pair_reply = await asyncio.wait_for(
                bus.call(
                    Message(
                        destination=BLUEZ_SERVICE,
                        path=device_path,
                        interface=DEVICE_IFACE,
                        member="Pair",
                    )
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _LOGGER.error(
                "PIN pairing timed out after %.0fs — agent responded: %s. "
                "If agent never responded, BlueZ may not have reached the "
                "passkey exchange (check gateway is discoverable/bondable).",
                timeout, agent.responded,
            )
            return False
        except Exception as exc:
            _LOGGER.error("D-Bus Pair() call failed: %s (type: %s)", exc, type(exc).__name__)
            return False

        if pair_reply.message_type == MessageType.ERROR:
            error_name = pair_reply.error_name or ""
            if "AlreadyExists" in error_name:
                _LOGGER.info("Device already paired (AlreadyExists)")
                return True
            if "AuthenticationFailed" in error_name:
                _LOGGER.error(
                    "PIN pairing AuthenticationFailed — agent responded: %s. "
                    "If agent responded=True, the PIN was sent but rejected "
                    "(wrong PIN?). If responded=False, BlueZ never asked for "
                    "the PIN (pairing method mismatch?).",
                    agent.responded,
                )
                return False
            _LOGGER.error(
                "PIN pairing failed: %s — %s", error_name, pair_reply.body
            )
            return False

        _LOGGER.info(
            "PIN pairing successful for %s (agent responded: %s)",
            device_address,
            agent.responded,
        )
        return True

    finally:
        # ── Cleanup ───────────────────────────────────────────────────
        if agent_registered and bus:
            try:
                await bus.call(
                    Message(
                        destination=BLUEZ_SERVICE,
                        path="/org/bluez",
                        interface=AGENT_MANAGER_IFACE,
                        member="UnregisterAgent",
                        signature="o",
                        body=[AGENT_PATH],
                    )
                )
                _LOGGER.debug("PIN agent unregistered")
            except Exception as exc:
                _LOGGER.debug("Agent cleanup error: %s", exc)
        if bus:
            bus.disconnect()


async def pair_push_button(
    device_address: str,
    timeout: float = 30.0,
) -> bool:
    """Register a temporary D-Bus agent for Just Works pairing, pair, clean up.

    PushButton (newer) gateways use Just Works BLE pairing — no PIN is
    exchanged at the BLE level.  However BlueZ still requires an agent
    to accept the RequestConfirmation / RequestAuthorization callbacks.
    Without an agent, BlueZ rejects the pairing with AuthenticationFailed.

    This mirrors the Android flow where ``createBond()`` succeeds
    automatically after the user presses the physical Connect button.

    Returns True if pairing succeeded or device was already bonded.
    """
    if not _DBUS_AVAILABLE:
        _LOGGER.error(
            "PushButton pairing requires Linux/HAOS with BlueZ and dbus-fast — "
            "not available on %s",
            platform.system(),
        )
        return False

    from dbus_fast import BusType, Message, MessageType  # noqa: F811
    from dbus_fast.aio import MessageBus  # noqa: F811

    bus: Any = None
    agent_registered = False

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        _LOGGER.error("Cannot connect to system D-Bus: %s", exc)
        return False

    try:
        # ── Find device in BlueZ object tree ──────────────────────────
        device_path = await _find_device_path(bus, device_address)
        if not device_path:
            _LOGGER.warning(
                "Device %s not found in BlueZ — cannot D-Bus pair",
                device_address,
            )
            return False

        # ── Check if already bonded ───────────────────────────────────
        if await _is_paired(bus, device_path):
            _LOGGER.info(
                "Device %s already bonded — skipping PushButton pairing",
                device_address,
            )
            return True

        # ── Register agent (passkey=0, pin="" — Just Works) ──────────
        agent = _PinAgentInterface(0, "")
        bus.export(AGENT_PATH, agent)

        # Use NoInputNoOutput capability for Just Works pairing
        agent_registered = await _register_agent_no_input(bus)
        if not agent_registered:
            _LOGGER.error("Failed to register Just Works agent with BlueZ")
            return False

        _LOGGER.info(
            "Just Works agent registered — initiating PushButton pairing with %s",
            device_address,
        )

        # ── Call Device1.Pair() ───────────────────────────────────────
        try:
            pair_reply = await asyncio.wait_for(
                bus.call(
                    Message(
                        destination=BLUEZ_SERVICE,
                        path=device_path,
                        interface=DEVICE_IFACE,
                        member="Pair",
                    )
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _LOGGER.error("PushButton pairing timed out after %.0fs", timeout)
            return False
        except Exception as exc:
            _LOGGER.error("D-Bus Pair() call failed: %s", exc)
            return False

        if pair_reply.message_type == MessageType.ERROR:
            error_name = pair_reply.error_name or ""
            if "AlreadyExists" in error_name:
                _LOGGER.info("Device already paired (AlreadyExists)")
                return True
            if "AuthenticationFailed" in error_name:
                _LOGGER.error(
                    "PushButton pairing AuthenticationFailed — is the gateway "
                    "Connect button pressed / pairing mode active?"
                )
                return False
            _LOGGER.error(
                "PushButton pairing failed: %s — %s", error_name, pair_reply.body
            )
            return False

        _LOGGER.info(
            "PushButton pairing successful for %s (agent responded: %s)",
            device_address,
            agent.responded,
        )
        return True

    finally:
        if agent_registered and bus:
            try:
                await bus.call(
                    Message(
                        destination=BLUEZ_SERVICE,
                        path="/org/bluez",
                        interface=AGENT_MANAGER_IFACE,
                        member="UnregisterAgent",
                        signature="o",
                        body=[AGENT_PATH],
                    )
                )
                _LOGGER.debug("Just Works agent unregistered")
            except Exception as exc:
                _LOGGER.debug("Agent cleanup error: %s", exc)
        if bus:
            bus.disconnect()


async def remove_bond(device_address: str) -> bool:
    """Remove an existing BlueZ bond for the given address.

    Useful when a bond is stale (gateway was factory-reset) and needs
    to be re-established with a fresh Pair() call.

    Returns True if the bond was removed, False otherwise.
    """
    if not _DBUS_AVAILABLE:
        return False

    from dbus_fast import BusType, Message, MessageType  # noqa: F811
    from dbus_fast.aio import MessageBus  # noqa: F811

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception:
        return False

    try:
        device_path = await _find_device_path(bus, device_address)
        if not device_path:
            _LOGGER.debug("Cannot remove bond — device %s not in BlueZ", device_address)
            return False

        # Extract adapter path (parent of device path)
        adapter_path = "/".join(device_path.split("/")[:-1])

        reply = await bus.call(
            Message(
                destination=BLUEZ_SERVICE,
                path=adapter_path,
                interface="org.bluez.Adapter1",
                member="RemoveDevice",
                signature="o",
                body=[device_path],
            )
        )

        if reply.message_type == MessageType.ERROR:
            _LOGGER.warning("RemoveDevice failed: %s %s", reply.error_name, reply.body)
            return False

        _LOGGER.info("Removed stale bond for %s", device_address)
        return True
    finally:
        bus.disconnect()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _find_device_path(bus: Any, address: str) -> str | None:
    """Find the BlueZ D-Bus object path for a device by MAC address."""
    from dbus_fast import Message, MessageType  # noqa: F811

    reply = await bus.call(
        Message(
            destination=BLUEZ_SERVICE,
            path="/",
            interface=OBJECT_MANAGER_IFACE,
            member="GetManagedObjects",
        )
    )

    if reply.message_type == MessageType.ERROR:
        _LOGGER.warning("Failed to query BlueZ managed objects: %s", reply.body)
        return None

    mac_suffix = address.upper().replace(":", "_")
    objects: dict = reply.body[0] if reply.body else {}

    for path_str, interfaces in objects.items():
        if mac_suffix in str(path_str) and DEVICE_IFACE in interfaces:
            _LOGGER.debug("BlueZ device found at: %s", path_str)
            return str(path_str)

    return None


async def _is_paired(bus: Any, device_path: str) -> bool:
    """Check if a BlueZ device is currently bonded."""
    from dbus_fast import Message, MessageType  # noqa: F811

    reply = await bus.call(
        Message(
            destination=BLUEZ_SERVICE,
            path=device_path,
            interface=PROPERTIES_IFACE,
            member="Get",
            signature="ss",
            body=[DEVICE_IFACE, "Paired"],
        )
    )

    if reply.message_type == MessageType.ERROR:
        return False

    val = reply.body[0] if reply.body else None
    if hasattr(val, "value"):
        return bool(val.value)
    return bool(val)


async def _register_agent_no_input(bus: Any) -> bool:
    """Register agent with NoInputNoOutput capability (Just Works)."""
    from dbus_fast import Message, MessageType  # noqa: F811

    reply = await bus.call(
        Message(
            destination=BLUEZ_SERVICE,
            path="/org/bluez",
            interface=AGENT_MANAGER_IFACE,
            member="RegisterAgent",
            signature="os",
            body=[AGENT_PATH, "NoInputNoOutput"],
        )
    )

    if reply.message_type == MessageType.ERROR:
        error_name = reply.error_name or ""
        if "AlreadyExists" in error_name:
            _LOGGER.debug("Agent already exists — re-registering (NoInputNoOutput)")
            await bus.call(
                Message(
                    destination=BLUEZ_SERVICE,
                    path="/org/bluez",
                    interface=AGENT_MANAGER_IFACE,
                    member="UnregisterAgent",
                    signature="o",
                    body=[AGENT_PATH],
                )
            )
            reply = await bus.call(
                Message(
                    destination=BLUEZ_SERVICE,
                    path="/org/bluez",
                    interface=AGENT_MANAGER_IFACE,
                    member="RegisterAgent",
                    signature="os",
                    body=[AGENT_PATH, "NoInputNoOutput"],
                )
            )
            if reply.message_type == MessageType.ERROR:
                _LOGGER.error("Agent re-registration failed: %s", reply.body)
                return False
        else:
            _LOGGER.error("Agent registration failed: %s %s", error_name, reply.body)
            return False

    default_reply = await bus.call(
        Message(
            destination=BLUEZ_SERVICE,
            path="/org/bluez",
            interface=AGENT_MANAGER_IFACE,
            member="RequestDefaultAgent",
            signature="o",
            body=[AGENT_PATH],
        )
    )
    if default_reply.message_type == MessageType.ERROR:
        _LOGGER.debug(
            "RequestDefaultAgent failed (non-fatal): %s", default_reply.body
        )

    return True


async def _register_agent(bus: Any) -> bool:
    """Register our PIN agent with BlueZ AgentManager1."""
    from dbus_fast import Message, MessageType  # noqa: F811

    # Try to register
    reply = await bus.call(
        Message(
            destination=BLUEZ_SERVICE,
            path="/org/bluez",
            interface=AGENT_MANAGER_IFACE,
            member="RegisterAgent",
            signature="os",
            body=[AGENT_PATH, "KeyboardDisplay"],
        )
    )

    if reply.message_type == MessageType.ERROR:
        error_name = reply.error_name or ""
        if "AlreadyExists" in error_name:
            # Re-register: unregister first, then register again
            _LOGGER.debug("Agent already exists — re-registering")
            await bus.call(
                Message(
                    destination=BLUEZ_SERVICE,
                    path="/org/bluez",
                    interface=AGENT_MANAGER_IFACE,
                    member="UnregisterAgent",
                    signature="o",
                    body=[AGENT_PATH],
                )
            )
            reply = await bus.call(
                Message(
                    destination=BLUEZ_SERVICE,
                    path="/org/bluez",
                    interface=AGENT_MANAGER_IFACE,
                    member="RegisterAgent",
                    signature="os",
                    body=[AGENT_PATH, "KeyboardDisplay"],
                )
            )
            if reply.message_type == MessageType.ERROR:
                _LOGGER.error("Agent re-registration failed: %s", reply.body)
                return False
        else:
            _LOGGER.error("Agent registration failed: %s %s", error_name, reply.body)
            return False

    # Request to be the default agent (highest priority)
    default_reply = await bus.call(
        Message(
            destination=BLUEZ_SERVICE,
            path="/org/bluez",
            interface=AGENT_MANAGER_IFACE,
            member="RequestDefaultAgent",
            signature="o",
            body=[AGENT_PATH],
        )
    )
    if default_reply.message_type == MessageType.ERROR:
        _LOGGER.debug(
            "RequestDefaultAgent failed (non-fatal): %s", default_reply.body
        )

    return True
