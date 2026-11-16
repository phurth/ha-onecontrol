"""Stable identity for per-device OneControl entities.

Background
----------
The gateway addresses every device as ``(table_id, device_id)``.  Neither half
is stable: ``table_id`` changes whenever the gateway rebuilds its device table,
and ``device_id`` is only the row number inside that table, so devices can move
to different rows after a rebuild.  Entity unique IDs built from the address
therefore end up pointing at a different physical device after a renumbering
(a "Porch Light" entity silently starts driving the kitchen lights).

What stays constant is what the device *is*: the ``function_name`` /
``function_instance`` pair reported by GetDevicesMetadata.  The coordinator
turns that pair into an identity token (``fn0030_00``) once a table's metadata
has been committed in full.  Entities use the token for their unique ID and
resolve their current ``(table_id, device_id)`` through the coordinator every
time they need it, so they follow the device across renumberings.

Fallback
--------
Gateways that never deliver metadata (IDS-CAN BLE gateways, or a MyRvLink
gateway that keeps rejecting the request) keep the previous address-based
unique IDs and behave exactly as before.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .helpers import is_valid_device_id

if TYPE_CHECKING:
    from .coordinator import OneControlCoordinator

_LOGGER = logging.getLogger(__name__)


def legacy_unique_id(
    mac: str, kind: str, table_id: int, device_id: int, *, with_table: bool = False
) -> str:
    """Return the address-based unique ID used before identity tokens."""
    if with_table:
        return f"{mac}_{kind}_{table_id:02x}_{device_id:02x}"
    return f"{mac}_{kind}_{device_id:02x}"


class OneControlDeviceEntity:
    """Mixin for entities that represent one device on the RV bus.

    Call :meth:`_bind_device` from ``__init__`` (after the coordinator has been
    stored by ``CoordinatorEntity.__init__``).  ``_table_id``, ``_device_id``
    and ``_key`` are then properties that always reflect the device's *current*
    address.
    """

    _oc_ident: str | None = None
    _oc_origin: tuple[int, int] = (0, 0)
    _oc_legacy_unique_id: str = ""

    def _bind_device(
        self,
        address: str,
        table_id: int,
        device_id: int,
        kind: str,
        *,
        legacy_with_table: bool = False,
    ) -> None:
        """Record the device this entity represents and set its unique ID."""
        mac = address.replace(":", "").lower()
        self._oc_origin = (table_id, device_id)
        self._oc_ident = self.coordinator.entity_identity(table_id, device_id)
        self._oc_legacy_unique_id = legacy_unique_id(
            mac, kind, table_id, device_id, with_table=legacy_with_table
        )
        if self._oc_ident is not None:
            self._attr_unique_id = f"{mac}_{kind}_{self._oc_ident}"
        else:
            self._attr_unique_id = self._oc_legacy_unique_id

    # ── Current address ───────────────────────────────────────────────

    def _oc_address(self) -> tuple[int, int]:
        if self._oc_ident is not None:
            current = self.coordinator.identity_address(self._oc_ident)
            if current is not None:
                return current
        return self._oc_origin

    @property
    def _table_id(self) -> int:
        return self._oc_address()[0]

    @property
    def _device_id(self) -> int:
        return self._oc_address()[1]

    @property
    def _key(self) -> str:
        table_id, device_id = self._oc_address()
        return f"{table_id:02x}:{device_id:02x}"

    # ── Registry hand-over ────────────────────────────────────────────

    @callback
    def async_claim_legacy_entry(self, hass: HomeAssistant, domain: str) -> None:
        """Move an existing address-based registry entry onto the identity ID.

        Runs just before the entity is added.  If the registry still holds the
        old address-based unique ID for the address this device occupies right
        now, that entry is re-keyed so the user keeps the same entity_id,
        history, area and customisations instead of getting a second entity.
        """
        new_uid = self._attr_unique_id
        legacy_uid = self._oc_legacy_unique_id
        live = self.coordinator.live_legacy_unique_ids

        if self._oc_ident is None or new_uid is None:
            # Address-based entity: remember it so nothing re-keys its entry
            # while it is loaded.
            live.add((domain, legacy_uid))
            return
        if (domain, legacy_uid) in live:
            return

        registry = er.async_get(hass)
        if registry.async_get_entity_id(domain, DOMAIN, new_uid) is not None:
            return
        entity_id = registry.async_get_entity_id(domain, DOMAIN, legacy_uid)
        if entity_id is None:
            return
        registry.async_update_entity(entity_id, new_unique_id=new_uid)
        _LOGGER.info(
            "Re-keyed %s from address-based unique_id %s to %s",
            entity_id,
            legacy_uid,
            new_uid,
        )


class DeviceEntityAdder:
    """Create per-device entities exactly once per logical device.

    Platforms call :meth:`offer` for every status event and when sweeping the
    coordinator's state.  The adder waits until the coordinator can name the
    device, then creates the entities and remembers both the address and the
    identity so a later renumbering does not create a second entity.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: OneControlCoordinator,
        address: str,
        async_add_entities: Any,
        domain: str,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._address = address
        self._async_add_entities = async_add_entities
        self._domain = domain
        self._seen: set[tuple[str, str]] = set()

    @callback
    def build(
        self,
        slot: str,
        table_id: int,
        device_id: int,
        *classes: type,
    ) -> list[Any]:
        """Return new entities for the device, or ``[]`` if none are due.

        ``slot`` namespaces the bookkeeping per device kind ("relay", "tank", …).
        """
        if not is_valid_device_id(device_id):
            return []
        coordinator = self._coordinator
        address_token = (slot, f"{table_id:02x}:{device_id:02x}")
        if address_token in self._seen:
            return []
        if not coordinator.device_entity_ready(table_id, device_id):
            return []

        ident = coordinator.entity_identity(table_id, device_id)
        if ident is not None:
            ident_token = (slot, ident)
            if ident_token in self._seen:
                # Same logical device at a new address.  The existing entity
                # follows it; just remember the address.
                self._seen.add(address_token)
                return []
            self._seen.add(ident_token)
        self._seen.add(address_token)

        entities = [
            cls(coordinator, self._address, table_id, device_id) for cls in classes
        ]
        for entity in entities:
            entity.async_claim_legacy_entry(self._hass, self._domain)
        return entities

    @callback
    def offer(
        self,
        slot: str,
        table_id: int,
        device_id: int,
        *classes: type,
    ) -> None:
        """Create and add entities for the device if they are due."""
        entities = self.build(slot, table_id, device_id, *classes)
        if entities:
            self._async_add_entities(entities)
