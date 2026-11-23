"""Keep a bonded gateway's connection on the adapter that holds its bond.

Home Assistant's Bluetooth stack (habluetooth) chooses the connection path for
every ``BleakClient`` itself: its client wrapper keeps only the *address* of
the ``BLEDevice`` it is given and scores every scanner that has heard the
device (signal strength, free connection slots, connects in progress, recent
failures).  Nothing an integration passes in selects the adapter, and Home
Assistant has no per-device pin (habluetooth issues #602 and #628).

BlueZ keeps a bond — the long-term key — per adapter.  For a gateway that
needs a bonded, encrypted link, only that adapter can authenticate: a second
local adapter or an ESPHome proxy gets "insufficient authentication", and on a
second local adapter a GATT read can even hang once the gateway drops the
unencrypted link.

``PinnedHaBleakClient`` (built by :func:`pinned_client_class`) subclasses Home
Assistant's client wrapper and narrows only its path choice to the adapter(s)
holding the bond.  Slot accounting, connect tracking, connection parameters
and failure counting all stay with habluetooth.  When the bonded adapter cannot
take the connection right now (it has not heard the device recently, or has no
free connection slot) the connect fails with :class:`PinnedAdapterUnavailable`
and is retried later, instead of going through a path that cannot work.

This module has no Home Assistant imports so it can be unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from bleak.exc import BleakError

_LOGGER = logging.getLogger(__name__)

NO_RSSI = -127

# The two habluetooth methods the pinned client relies on.  Both are private,
# so their presence is checked before pinning is used at all.
_SELECT_METHOD = "_async_get_best_available_backend_and_device"
_BACKEND_METHOD = "_async_get_backend_for_ble_device"


class PinnedAdapterUnavailable(BleakError):
    """The adapter holding the bond cannot take this connection right now."""


def normalize_source(source: Any) -> str:
    """Return *source* (adapter MAC or proxy id) in comparable form."""
    return str(source).upper().replace(":", "")


def pinned_candidates(candidates: list[Any], pinned: frozenset[str]) -> list[Any]:
    """Return the scanner candidates on a pinned source, strongest first.

    *candidates* are habluetooth ``BluetoothScannerDevice`` objects (anything
    with ``.scanner.source`` and ``.advertisement.rssi``).
    """
    matching = [
        candidate
        for candidate in candidates
        if normalize_source(candidate.scanner.source) in pinned
    ]
    return sorted(
        matching,
        key=lambda candidate: getattr(candidate.advertisement, "rssi", None)
        or NO_RSSI,
        reverse=True,
    )


def _describe(candidate: Any) -> str:
    scanner = candidate.scanner
    name = getattr(scanner, "name", None) or scanner.source
    rssi = getattr(candidate.advertisement, "rssi", None)
    return f"{name} (RSSI {rssi})"


def _make_pinned_client_class(base: type) -> type:
    """Build the pinned client as a subclass of Home Assistant's wrapper *base*."""

    class PinnedHaBleakClient(base):  # type: ignore[misc, valid-type]
        """Home Assistant's BleakClient wrapper, restricted to pinned sources."""

        def __init__(
            self,
            address_or_ble_device: Any,
            *args: Any,
            pinned_sources: Any = (),
            **kwargs: Any,
        ) -> None:
            self._oc_pinned = frozenset(normalize_source(s) for s in pinned_sources)
            self._oc_address = str(
                getattr(address_or_ble_device, "address", address_or_ble_device)
            )
            super().__init__(address_or_ble_device, *args, **kwargs)

        def _async_get_best_available_backend_and_device(
            self, *args: Any, **kwargs: Any
        ) -> Any:
            if not self._oc_pinned:
                return super()._async_get_best_available_backend_and_device(
                    *args, **kwargs
                )
            manager = args[0] if args else kwargs.get("manager")
            if manager is None:  # signature without the manager argument
                from habluetooth import get_manager  # noqa: PLC0415

                manager = get_manager()
            address = self._oc_address
            candidates = list(manager.async_scanner_devices_by_address(address, True))
            usable = pinned_candidates(candidates, self._oc_pinned)
            others = [c for c in candidates if c not in usable]
            for candidate in usable:
                backend = getattr(self, _BACKEND_METHOD)(
                    manager, candidate.scanner, candidate.ble_device
                )
                if backend is not None:
                    _LOGGER.info(
                        "%s: connecting through %s, the adapter holding the bond%s",
                        address,
                        _describe(candidate),
                        (
                            "; not using " + ", ".join(_describe(c) for c in others)
                            if others
                            else ""
                        ),
                    )
                    return backend
            pinned_text = ", ".join(sorted(self._oc_pinned))
            if usable:
                msg = (
                    f"{address}: the adapter holding the bond ({pinned_text}) "
                    "has no free connection slot"
                )
            else:
                msg = (
                    f"{address}: the adapter holding the bond ({pinned_text}) "
                    "has not heard the device recently"
                )
            raise PinnedAdapterUnavailable(msg)

    return PinnedHaBleakClient


_CLIENT_CLASS_CACHE: dict[str, type | None] = {}


def pinned_client_class() -> type | None:
    """Return the pinned client class, or None if pinning is not possible here.

    None means Home Assistant's Bluetooth wrapper is unavailable, or no longer
    has the methods the pin relies on.  Callers then connect the normal way and
    rely on :func:`connection_path` to notice a wrong path.
    """
    if "cls" not in _CLIENT_CLASS_CACHE:
        cls: type | None = None
        try:
            from habluetooth.wrappers import HaBleakClientWrapper  # noqa: PLC0415
        except ImportError:  # pragma: no cover - always present in Home Assistant
            _LOGGER.debug("habluetooth not available; adapter pinning disabled")
        else:
            if hasattr(HaBleakClientWrapper, _SELECT_METHOD) and hasattr(
                HaBleakClientWrapper, _BACKEND_METHOD
            ):
                cls = _make_pinned_client_class(HaBleakClientWrapper)
            else:
                _LOGGER.warning(
                    "This Home Assistant version's Bluetooth client no longer has "
                    "the hooks used to keep a bonded gateway on its adapter; "
                    "connections will go wherever Home Assistant routes them"
                )
        _CLIENT_CLASS_CACHE["cls"] = cls
    return _CLIENT_CLASS_CACHE["cls"]


@dataclass(frozen=True)
class ConnectionPath:
    """Where a connected client's link actually goes."""

    source: str | None
    """Home Assistant scanner source: adapter MAC, or a proxy's id."""

    device_path: str | None
    """BlueZ device object path (``/org/bluez/hciN/dev_…``) for a local link."""

    def describe(self) -> str:
        """Return a short human-readable form for logs."""
        if self.device_path and self.source:
            return f"{self.source} ({self.device_path.rsplit('/', 1)[0]})"
        return self.source or self.device_path or "an unknown path"


def connection_path(client: Any) -> ConnectionPath:
    """Return the path a connected client actually uses (best effort).

    Reads habluetooth's record of the scanner it connected through and, for
    a local link, bleak's BlueZ device object path.  Either may be missing on
    other library versions; the fields are then None.
    """
    scanner = getattr(client, "_connected_scanner", None)
    source = getattr(scanner, "source", None) if scanner is not None else None
    backend = getattr(client, "_backend", None)
    device_path = getattr(backend, "_device_path", None)
    return ConnectionPath(
        source if isinstance(source, str) else None,
        device_path if isinstance(device_path, str) else None,
    )


def path_matches_pin(
    path: ConnectionPath,
    pinned_sources: frozenset[str],
    pinned_device_paths: frozenset[str],
) -> bool | None:
    """Return whether *path* is a pinned one, or None when it cannot be told.

    The BlueZ device path is decisive when both it and the pinned paths are
    known.  Otherwise the scanner source decides: pins are local adapter MACs,
    so a proxy's source never matches.
    """
    if path.device_path is not None and pinned_device_paths:
        return path.device_path in pinned_device_paths
    if path.source is not None:
        return normalize_source(path.source) in pinned_sources
    return None
