"""Stable device identity across gateway renumbering.

The scenario mirrors a real coach: the gateway rebuilt its device table
(0xDB → 0xDC) and every device moved down two rows.  Address-based unique IDs
then pointed existing entities at different devices and created duplicates.
"""

from __future__ import annotations

import asyncio
import importlib
import re
import sys
from types import SimpleNamespace

import pytest

from custom_components.ha_onecontrol import coordinator as coordinator_module
from custom_components.ha_onecontrol.coordinator import (
    MetadataCommitted,
    OneControlCoordinator,
)
from custom_components.ha_onecontrol.protocol.events import (
    CoverStatus,
    DeviceMetadata,
    DimmableLight,
    GatewayInformation,
    GeneratorStatus,
    HourMeter,
    HvacZone,
    RelayStatus,
    TankLevel,
)

ADDRESS = "AA:BB:CC:DD:EE:02"
MAC = "aabbccddee02"
PLATFORM_DOMAIN = "ha_onecontrol"

# (function_name, function_instance, kind, friendly name) by row order.
# Function codes are the ones the gateway reported for this coach.
DEVICES = [
    (98, 0, "cover", "Bedroom Slide"),
    (403, 0, None, "Base Camp Touchscreen"),
    (95, 0, "generator", "Generator"),
    (194, 0, "zone", "Garage Climate Zone"),
    (193, 0, "zone", "Bedroom Climate Zone"),
    (192, 0, "zone", "Main Climate Zone"),
    (95, 0, "hour_meter", "Generator"),
    (270, 0, "relay", "Tank Heater"),
    (5, 0, "relay", "Water Pump"),
    (49, 0, "relay", "Awning Light"),
    (70, 0, "tank", "Fuel Tank"),
    (69, 2, "tank", "Black Tank 2"),
    (69, 1, "tank", "Black Tank 1"),
    (67, 0, "tank", "Fresh Tank"),
    (180, 0, "dim", "Bunk Light"),
    (48, 0, "dim", "Porch Light"),
    (40, 0, "dim", "Kitchen Under Cabinet Light"),
]


def table_layout(first_row: int) -> dict[int, tuple[int, int, str | None, str]]:
    """Return device_id → device for a table whose first device sits at *first_row*."""
    return {first_row + i: dev for i, dev in enumerate(DEVICES)}


# ── Minimal Home Assistant stand-ins ──────────────────────────────────────


class FakeRegistry:
    """Entity registry keyed the way HA keys it: (domain, platform, unique_id)."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str, str], str] = {}

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str) -> str | None:
        return self.entries.get((domain, platform, unique_id))

    def async_update_entity(self, entity_id: str, *, new_unique_id: str) -> None:
        for key, value in list(self.entries.items()):
            if value == entity_id:
                assert (key[0], key[1], new_unique_id) not in self.entries
                del self.entries[key]
                self.entries[(key[0], key[1], new_unique_id)] = entity_id
                return
        raise KeyError(entity_id)

    def register(self, domain: str, unique_id: str, suggested: str) -> str:
        key = (domain, PLATFORM_DOMAIN, unique_id)
        if key not in self.entries:
            base = f"{domain}.{suggested}"
            entity_id, n = base, 2
            while entity_id in self.entries.values():
                entity_id, n = f"{base}_{n}", n + 1
            self.entries[key] = entity_id
        return self.entries[key]


class FakeCoordinatorEntity:
    def __init__(self, coordinator: object, *args: object, **kwargs: object) -> None:
        self.coordinator = coordinator

    @classmethod
    def __class_getitem__(cls, item: object) -> type:
        return cls

    @property
    def available(self) -> bool:
        return True

    def async_write_ha_state(self) -> None:
        pass


class FakeHomeAssistantError(Exception):
    pass


class _FakeEntity:
    """Base for SwitchEntity, LightEntity, …"""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class Platform:
    """Collects entities the way HA's entity platform does."""

    def __init__(self, registry: FakeRegistry, domain: str) -> None:
        self.registry = registry
        self.domain = domain
        self.loaded: dict[str, object] = {}  # unique_id → entity
        self.rejected: list[str] = []

    def add(self, entities: list) -> None:
        for entity in entities:
            uid = entity._attr_unique_id
            if uid in self.loaded:
                # HA: "Platform does not generate unique IDs" — entity ignored.
                self.rejected.append(uid)
                continue
            name = getattr(entity, "name", None) or getattr(entity, "_attr_name", uid)
            entity.entity_id = self.registry.register(self.domain, uid, _slug(str(name)))
            self.loaded[uid] = entity

    def by_entity_id(self, entity_id: str) -> object:
        return next(e for e in self.loaded.values() if e.entity_id == entity_id)


PLATFORM_MODULES = ("switch", "light", "climate", "sensor", "binary_sensor", "cover")


@pytest.fixture(scope="module")
def platforms():
    """Import the entity platforms against real (if minimal) base classes."""
    pkg = "custom_components.ha_onecontrol"
    saved_modules = {
        name: sys.modules.get(name)
        for name in [f"{pkg}.{m}" for m in (*PLATFORM_MODULES, "entity")]
    }
    saved_attrs: list[tuple[object, str, object]] = []

    def patch(module_name: str, attr: str, value: object) -> None:
        module = sys.modules.get(module_name)
        if module is None:
            module = type(sys.modules["homeassistant.core"])(module_name)
            sys.modules[module_name] = module
            saved_modules.setdefault(module_name, None)
        saved_attrs.append((module, attr, module.__dict__.get(attr, _MISSING)))
        setattr(module, attr, value)

    patch("homeassistant.helpers.update_coordinator", "CoordinatorEntity", FakeCoordinatorEntity)
    patch("homeassistant.exceptions", "HomeAssistantError", FakeHomeAssistantError)
    for component, cls in (
        ("switch", "SwitchEntity"),
        ("light", "LightEntity"),
        ("climate", "ClimateEntity"),
        ("sensor", "SensorEntity"),
        ("binary_sensor", "BinarySensorEntity"),
        ("cover", "CoverEntity"),
    ):
        patch(f"homeassistant.components.{component}", cls, type(cls, (_FakeEntity,), {}))

    for name in saved_modules:
        if name.startswith(pkg):
            sys.modules.pop(name, None)
    loaded = {m: importlib.import_module(f"{pkg}.{m}") for m in PLATFORM_MODULES}
    yield loaded

    for module, attr, value in saved_attrs:
        if value is _MISSING:
            module.__dict__.pop(attr, None)
        else:
            setattr(module, attr, value)
    for name, module in saved_modules.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


_MISSING = object()


# ── Simulated gateway ─────────────────────────────────────────────────────


class _Coordinator(OneControlCoordinator):
    data_healthy = True

    def async_add_listener(self, update_callback, context=None):
        self._test_listeners.append(update_callback)
        return lambda: self._test_listeners.remove(update_callback)

    def async_update_listeners(self) -> None:
        for listener in list(self._test_listeners):
            listener()


def make_coordinator() -> OneControlCoordinator:
    c = object.__new__(_Coordinator)
    c.gateway_info = None
    c.device_names = {}
    c._metadata_raw = {}
    c._metadata_loaded_tables = set()
    c._last_metadata_crc = None
    c._identity_tables = set()
    c._identity_by_key = {}
    c._identity_address = {}
    c._identity_mode = None
    c._identity_wait_started = {}
    c._identity_wait_warned = set()
    c._identity_entities_exist = None
    c._identity_last_by_key = {}
    c._identity_last_address = {}
    c._pending_hvac, c._hvac_zone_states, c._hvac_retry_handles = {}, {}, {}
    c._last_known_dimmable_brightness, c._last_known_rgb_color = {}, {}
    c.device_online, c.device_locks = {}, {}
    c.observed_hvac_capability, c._last_dtc_codes = {}, {}
    c.hass = None
    c.entry = SimpleNamespace(entry_id="e1")
    c._test_listeners = []
    c.bonded_adapters = {}
    c.connection_source = None
    c.fallback_source = None
    c._pinned_sources = frozenset()
    c.live_legacy_unique_ids = set()
    c._can_ble_confirmed = False
    c._event_callbacks = []
    c.relays, c.generators, c.hour_meters = {}, {}, {}
    c.dimmable_lights, c.rgb_lights = {}, {}
    c.hvac_zones, c.tanks, c.covers = {}, {}, {}
    c.levelers, c.tank_alerts = {}, {}
    c.async_set_updated_data = lambda data: None
    c._build_data = lambda: {}
    c.sent = []

    async def async_switch(table_id: int, device_id: int, state: bool) -> None:
        c.sent.append(("switch", table_id, device_id, state))

    c.async_switch = async_switch
    return c


class Rig:
    """A coordinator with all entity platforms attached."""

    def __init__(self, platforms: dict, registry: FakeRegistry | None = None) -> None:
        self.registry = registry or FakeRegistry()
        self.coordinator = make_coordinator()
        self.platforms = {m: Platform(self.registry, m) for m in PLATFORM_MODULES}
        entry = SimpleNamespace(
            entry_id="e1",
            data={"address": ADDRESS},
            options={},
            async_on_unload=lambda remove: None,
        )
        hass = SimpleNamespace(data={PLATFORM_DOMAIN: {"e1": self.coordinator}})
        entity_module = sys.modules["custom_components.ha_onecontrol.entity"]
        entity_module.er.async_get = lambda _hass: self.registry
        coordinator_module.er.async_get = lambda _hass: self.registry
        coordinator_module.er.async_entries_for_config_entry = (
            lambda registry, _entry_id: [
                SimpleNamespace(unique_id=key[2]) for key in registry.entries
            ]
        )
        for name, module in platforms.items():
            asyncio.run(module.async_setup_entry(hass, entry, self.platforms[name].add))

    # Gateway behaviour -----------------------------------------------------

    def gateway(self, table_id: int) -> None:
        self.coordinator.gateway_info = GatewayInformation(
            protocol_version=6, device_count=len(DEVICES), table_id=table_id,
            device_metadata_table_crc=table_id,
        )

    def _emit(self, store: dict, event: object) -> None:
        store[f"{event.table_id:02x}:{event.device_id:02x}"] = event
        for cb in list(self.coordinator._event_callbacks):
            cb(event)

    def status(self, table_id: int, layout: dict) -> None:
        """Emit one status event per device, as the gateway does continuously."""
        c = self.coordinator
        for device_id, (_fn, _inst, kind, _name) in layout.items():
            if kind == "relay":
                self._emit(c.relays, RelayStatus(table_id=table_id, device_id=device_id))
            elif kind == "generator":
                self._emit(c.generators, GeneratorStatus(table_id=table_id, device_id=device_id))
            elif kind == "hour_meter":
                self._emit(c.hour_meters, HourMeter(table_id=table_id, device_id=device_id))
            elif kind == "zone":
                self._emit(c.hvac_zones, HvacZone(table_id=table_id, device_id=device_id))
            elif kind == "tank":
                self._emit(c.tanks, TankLevel(table_id=table_id, device_id=device_id, level=50))
            elif kind == "dim":
                self._emit(c.dimmable_lights, DimmableLight(table_id=table_id, device_id=device_id))
            elif kind == "cover":
                self._emit(c.covers, CoverStatus(table_id=table_id, device_id=device_id))

    def commit_metadata(self, table_id: int, layout: dict) -> None:
        """Deliver a complete GetDevicesMetadata response for a table."""
        c = self.coordinator
        for device_id, (fn, inst, _kind, name) in layout.items():
            c._metadata_raw[f"{table_id:02x}:{device_id:02x}"] = DeviceMetadata(
                table_id=table_id, device_id=device_id,
                function_name=fn, function_instance=inst,
            )
            c.device_names[f"{table_id:02x}:{device_id:02x}"] = name
        c._metadata_loaded_tables.add(table_id)
        c._commit_identity_table(table_id)

    def invalidate_metadata(self, table_id: int) -> None:
        """Mimic the "Metadata CRC changed … invalidating table" path."""
        c = self.coordinator
        prefix = f"{table_id:02x}:"
        for key in [k for k in c._metadata_raw if k.startswith(prefix)]:
            del c._metadata_raw[key]
            c.device_names.pop(key, None)
        c._suspend_identity_table(table_id)
        c._metadata_loaded_tables.discard(table_id)

    def boot(self, table_id: int, layout: dict) -> None:
        self.gateway(table_id)
        self.status(table_id, layout)
        self.commit_metadata(table_id, layout)
        self.status(table_id, layout)

    # Inspection ------------------------------------------------------------

    def all_entity_ids(self) -> set[str]:
        return set(self.registry.entries.values())

    def entity(self, entity_id: str) -> object:
        return self.platforms[entity_id.split(".")[0]].by_entity_id(entity_id)


# Before and after the gateway rebuilt its table.  Only the two-row shift
# matters: the Water Pump sits at 0x0B in the first table and 0x09 in the second.
TABLE_DB = table_layout(0x03)
TABLE_DC = table_layout(0x01)


# ── Tests ─────────────────────────────────────────────────────────────────


def test_entities_wait_for_metadata_and_use_function_based_ids(platforms) -> None:
    rig = Rig(platforms)
    rig.gateway(0xDB)
    rig.status(0xDB, TABLE_DB)
    assert rig.all_entity_ids() - _static_ids(rig) == set(), "no device entities before metadata"

    rig.commit_metadata(0xDB, TABLE_DB)

    assert rig.coordinator.identity_mode == "identity"
    pump = rig.entity("switch.water_pump")
    assert pump._attr_unique_id == f"{MAC}_switch_fn0005_00"
    assert rig.entity("light.porch_light")._attr_unique_id == f"{MAC}_light_fn0030_00"
    # No entity is ever named by its address.
    assert not [e for e in rig.all_entity_ids() if "device_d" in e]


def test_renumbering_keeps_every_entity_on_its_device(platforms) -> None:
    rig = Rig(platforms)
    rig.boot(0xDB, TABLE_DB)
    before = dict(rig.registry.entries)
    pump = rig.entity("switch.water_pump")
    heater = rig.entity("switch.tank_heater")
    porch = rig.entity("light.porch_light")
    assert (pump._table_id, pump._device_id) == (0xDB, 0x0B)

    # Gateway rebuilds its table; every device moves down two rows.
    rig.gateway(0xDC)
    rig.status(0xDC, TABLE_DC)
    rig.commit_metadata(0xDC, TABLE_DC)
    rig.status(0xDC, TABLE_DC)

    assert rig.registry.entries == before, "no entities created, removed or re-keyed"
    assert all(not p.rejected for p in rig.platforms.values())
    assert (pump._table_id, pump._device_id) == (0xDC, 0x09)
    assert pump.name == "Water Pump"
    assert heater.name == "Tank Heater"
    assert porch.name == "Porch Light"
    assert pump.available

    asyncio.run(pump.async_turn_on())
    assert rig.coordinator.sent == [("switch", 0xDC, 0x09, True)]


def test_events_from_a_new_table_never_create_address_based_entities(platforms, monkeypatch) -> None:
    rig = Rig(platforms)
    rig.boot(0xDB, TABLE_DB)
    before = dict(rig.registry.entries)

    clock = [1000.0]
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: clock[0])
    rig.gateway(0xDC)
    rig.status(0xDC, TABLE_DC)
    clock[0] += 600  # metadata for the new table is very late
    rig.status(0xDC, TABLE_DC)
    assert rig.registry.entries == before

    rig.commit_metadata(0xDC, TABLE_DC)
    assert rig.registry.entries == before
    assert rig.entity("switch.water_pump")._key == "dc:09"


def test_upgrade_rekeys_existing_address_based_entries(platforms) -> None:
    registry = FakeRegistry()
    # Registry as written by the previous release, addresses as in TABLE_DC.
    registry.entries = {
        ("switch", PLATFORM_DOMAIN, f"{MAC}_switch_09"): "switch.camper_water_pump",
        ("switch", PLATFORM_DOMAIN, f"{MAC}_switch_08"): "switch.camper_tank_heater",
        ("light", PLATFORM_DOMAIN, f"{MAC}_light_10"): "light.camper_porch",
        ("sensor", PLATFORM_DOMAIN, f"{MAC}_tank_0e"): "sensor.camper_fresh",
        ("climate", PLATFORM_DOMAIN, f"{MAC}_climate_06"): "climate.camper_main",
        ("switch", PLATFORM_DOMAIN, f"{MAC}_gen_switch_03"): "switch.camper_generator",
    }
    rig = Rig(platforms, registry)
    rig.boot(0xDC, TABLE_DC)

    assert rig.entity("switch.camper_water_pump").name == "Water Pump"
    assert rig.entity("switch.camper_tank_heater").name == "Tank Heater"
    assert rig.entity("light.camper_porch").name == "Porch Light"
    assert rig.entity("climate.camper_main").name == "Main Climate Zone"
    assert rig.entity("switch.camper_generator").name == "Generator"
    assert registry.async_get_entity_id("sensor", PLATFORM_DOMAIN, f"{MAC}_tank_fn0043_00") == "sensor.camper_fresh"
    # Old address-based IDs are gone, and nothing was duplicated.
    assert not [k for k in registry.entries if re.search(r"_[0-9a-f]{2}$", k[2]) and "_fn" not in k[2]]
    ids = list(registry.entries.values())
    assert len(ids) == len(set(ids))
    assert "switch.water_pump" not in ids


def test_devices_sharing_a_function_get_distinct_tokens(platforms) -> None:
    rig = Rig(platforms)
    rig.boot(0xDC, TABLE_DC)
    c = rig.coordinator
    # Generator controller (row 3) and its hour meter (row 7) both report function 95.
    assert c._identity_by_key["dc:03"] == "fn005f_00"
    assert c._identity_by_key["dc:07"] == "fn005f_00_2"
    gen = rig.entity("switch.generator")
    assert gen._attr_unique_id == f"{MAC}_gen_switch_fn005f_00"
    hours = [e for e in rig.platforms["sensor"].loaded.values() if "hourmeter" in e._attr_unique_id]
    assert [e._attr_unique_id for e in hours] == [f"{MAC}_hourmeter_fn005f_00_2"]


def test_metadata_for_an_old_table_does_not_move_entities(platforms) -> None:
    rig = Rig(platforms)
    rig.boot(0xDB, TABLE_DB)
    rig.gateway(0xDC)
    rig.status(0xDC, TABLE_DC)
    rig.commit_metadata(0xDC, TABLE_DC)
    pump = rig.entity("switch.water_pump")
    assert pump._key == "dc:09"

    # "Refresh Metadata" re-requests every table ever seen, including 0xDB.
    rig.commit_metadata(0xDB, TABLE_DB)
    assert pump._key == "dc:09"


def test_falls_back_to_address_ids_when_metadata_never_arrives(platforms, monkeypatch) -> None:
    rig = Rig(platforms)
    clock = [1000.0]
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: clock[0])
    rig.gateway(0xDC)
    rig.status(0xDC, TABLE_DC)
    assert rig.all_entity_ids() - _static_ids(rig) == set()

    clock[0] += coordinator_module._IDENTITY_GRACE_S + 1
    rig.status(0xDC, TABLE_DC)

    assert rig.coordinator.identity_mode == "legacy"
    uids = {k[2] for k in rig.registry.entries}
    assert f"{MAC}_switch_09" in uids
    assert not [u for u in uids if "_fn" in u]


def test_slow_metadata_never_duplicates_function_based_entities(platforms, monkeypatch) -> None:
    """Once an install uses function-based IDs, a slow start must not fall back."""
    registry = FakeRegistry()
    first = Rig(platforms, registry)
    first.boot(0xDC, TABLE_DC)
    before = dict(registry.entries)

    # Next start: metadata takes far longer than the grace period.
    second = Rig(platforms, registry)
    clock = [1000.0]
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: clock[0])
    second.gateway(0xDC)
    second.status(0xDC, TABLE_DC)
    clock[0] += 3600
    second.status(0xDC, TABLE_DC)
    assert second.coordinator.identity_mode is None
    assert registry.entries == before, "no address-based duplicates"

    second.commit_metadata(0xDC, TABLE_DC)
    assert registry.entries == before
    assert second.entity("switch.water_pump").name == "Water Pump"


def test_can_ble_gateways_keep_address_ids_without_delay(platforms) -> None:
    rig = Rig(platforms)
    rig.coordinator._can_ble_confirmed = True
    rig._emit(rig.coordinator.relays, RelayStatus(table_id=0, device_id=0x86))
    assert rig.coordinator.identity_mode == "legacy"
    assert (("switch", PLATFORM_DOMAIN, f"{MAC}_switch_86")) in rig.registry.entries


def test_removed_device_never_drives_the_device_that_took_its_row(platforms) -> None:
    """Same table_id, rebuilt metadata: the pump is gone and its row is reused."""
    rig = Rig(platforms)
    rig.boot(0xDC, TABLE_DC)
    pump = rig.entity("switch.water_pump")
    awning = rig.entity("switch.awning_light")
    fresh = rig.entity("sensor.fresh_tank_level")
    assert (pump._device_id, awning._device_id) == (0x09, 0x0A)
    before = dict(rig.registry.entries)

    # Water Pump is removed; every later device moves up one row, so the
    # Awning Light now sits where the pump was.
    devices = [d for d in DEVICES if d[3] != "Water Pump"]
    rebuilt = {0x01 + i: dev for i, dev in enumerate(devices)}
    rig.invalidate_metadata(0xDC)
    rig.commit_metadata(0xDC, rebuilt)
    rig.status(0xDC, rebuilt)

    assert rig.registry.entries == before, "nothing created or re-keyed"
    assert (awning._table_id, awning._device_id) == (0xDC, 0x09)
    assert awning.available
    assert fresh.available

    assert rig.coordinator.identity_address("fn0005_00") is None
    assert not pump.available
    assert pump._key not in rig.coordinator.relays
    with pytest.raises(FakeHomeAssistantError):
        asyncio.run(pump.async_turn_on())
    with pytest.raises(FakeHomeAssistantError):
        asyncio.run(pump.async_turn_off())
    assert rig.coordinator.sent == []

    # The pump comes back on a different row: the same entity resumes there.
    rig.invalidate_metadata(0xDC)
    restored = dict(rebuilt)
    restored[0x20] = (5, 0, "relay", "Water Pump")
    rig.commit_metadata(0xDC, restored)
    rig.status(0xDC, restored)
    assert rig.registry.entries == before
    assert pump.available and (pump._table_id, pump._device_id) == (0xDC, 0x20)
    asyncio.run(pump.async_turn_on())
    assert rig.coordinator.sent == [("switch", 0xDC, 0x20, True)]


class _Handle:
    cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def test_devices_are_out_of_service_while_metadata_is_refetched(platforms) -> None:
    """Between "Metadata CRC changed" and the next commit, rows are not trusted."""
    rig = Rig(platforms)
    rig.boot(0xDC, TABLE_DC)
    c = rig.coordinator
    before = dict(rig.registry.entries)
    devices = [
        rig.entity(eid) for eid in
        ("switch.water_pump", "light.porch_light", "climate.main_climate_zone",
         "cover.bedroom_slide", "sensor.fresh_tank_level")
    ]
    assert all(d.available for d in devices)
    retry = _Handle()
    c._pending_hvac["dc:06"] = object()
    c._hvac_retry_handles["dc:06"] = retry

    rig.invalidate_metadata(0xDC)

    assert not any(d.available for d in devices)
    for call in (
        rig.entity("switch.water_pump").async_turn_on(),
        rig.entity("light.porch_light").async_turn_off(),
        rig.entity("cover.bedroom_slide").async_open_cover(),
    ):
        with pytest.raises(FakeHomeAssistantError):
            asyncio.run(call)
    assert c.sent == []
    assert retry.cancelled and not c._pending_hvac and not c._hvac_retry_handles
    # Status frames keep arriving in the window; they must not create anything.
    rig.status(0xDC, TABLE_DC)
    assert rig.registry.entries == before

    rig.commit_metadata(0xDC, TABLE_DC)
    assert all(d.available for d in devices)
    assert rig.registry.entries == before
    assert rig.entity("switch.water_pump")._device_id == 0x09


def test_row_state_is_forgotten_when_its_occupant_changes(platforms) -> None:
    rig = Rig(platforms)
    rig.boot(0xDC, TABLE_DC)
    c = rig.coordinator
    stores = (
        c._hvac_zone_states, c._last_known_dimmable_brightness,
        c._last_known_rgb_color, c._pending_hvac,
    )
    for store in stores:
        store["dc:09"] = store["dc:08"] = object()

    rebuilt = dict(TABLE_DC)
    rebuilt[0x09] = (6, 0, "relay", "Gas Water Heater")
    rig.invalidate_metadata(0xDC)
    # Queued after the invalidation, before the new metadata arrives.
    retry_changed, retry_kept = _Handle(), _Handle()
    c._hvac_retry_handles.update({"dc:09": retry_changed, "dc:08": retry_kept})
    for store in stores:
        store["dc:09"] = store["dc:08"] = object()
    rig.commit_metadata(0xDC, rebuilt)

    assert retry_changed.cancelled and "dc:09" not in c._hvac_retry_handles
    assert not retry_kept.cancelled and "dc:08" in c._hvac_retry_handles
    for store in stores:
        assert "dc:09" not in store and "dc:08" in store


def test_new_device_on_a_reused_row_gets_its_own_entity(platforms) -> None:
    rig = Rig(platforms)
    rig.boot(0xDC, TABLE_DC)
    pump = rig.entity("switch.water_pump")
    before = set(rig.all_entity_ids())

    # The pump's function changes: to the integration it is a new device.
    rebuilt = dict(TABLE_DC)
    rebuilt[0x09] = (6, 0, "relay", "Gas Water Heater")
    rig.invalidate_metadata(0xDC)
    rig.commit_metadata(0xDC, rebuilt)
    rig.status(0xDC, rebuilt)

    assert rig.all_entity_ids() - before == {"switch.gas_water_heater"}
    heater = rig.entity("switch.gas_water_heater")
    assert heater._attr_unique_id == f"{MAC}_switch_fn0006_00"
    assert (heater._table_id, heater._device_id) == (0xDC, 0x09)
    assert not pump.available


def test_device_missing_from_the_new_table_is_unavailable(platforms) -> None:
    rig = Rig(platforms)
    rig.boot(0xDB, TABLE_DB)
    pump = rig.entity("switch.water_pump")

    devices = [d for d in DEVICES if d[3] != "Water Pump"]
    table_dc = {0x01 + i: dev for i, dev in enumerate(devices)}
    rig.gateway(0xDC)
    rig.commit_metadata(0xDC, table_dc)
    rig.status(0xDC, table_dc)

    assert not pump.available
    with pytest.raises(FakeHomeAssistantError):
        asyncio.run(pump.async_turn_on())
    assert rig.coordinator.sent == []


def test_registry_is_scanned_once_while_metadata_is_overdue(platforms, monkeypatch) -> None:
    registry = FakeRegistry()
    Rig(platforms, registry).boot(0xDC, TABLE_DC)

    second = Rig(platforms, registry)
    scans = [0]
    scan = coordinator_module.er.async_entries_for_config_entry

    def counting(reg: object, entry_id: str) -> list:
        scans[0] += 1
        return scan(reg, entry_id)

    monkeypatch.setattr(coordinator_module.er, "async_entries_for_config_entry", counting)
    clock = [1000.0]
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: clock[0])
    second.gateway(0xDC)
    second.status(0xDC, TABLE_DC)
    clock[0] += 3600
    for _ in range(5):
        second.status(0xDC, TABLE_DC)
    assert scans[0] == 1


def test_table_zero_device_settles_on_address_ids(platforms) -> None:
    rig = Rig(platforms)
    rig._emit(rig.coordinator.relays, RelayStatus(table_id=0, device_id=0x86))
    assert rig.coordinator.identity_mode == "legacy"
    assert (("switch", PLATFORM_DOMAIN, f"{MAC}_switch_86")) in rig.registry.entries


def test_metadata_commit_notifies_platforms(platforms) -> None:
    c = make_coordinator()
    seen: list[object] = []
    c._event_callbacks.append(seen.append)
    c.gateway_info = GatewayInformation(table_id=0xDC)
    c._metadata_raw["dc:09"] = DeviceMetadata(table_id=0xDC, device_id=9, function_name=5)
    c._commit_identity_table(0xDC)
    assert seen == [MetadataCommitted(table_id=0xDC)]
    assert c.identity_address("fn0005_00") == (0xDC, 9)


def _static_ids(rig: Rig) -> set[str]:
    """Entity IDs of the always-present gateway entities (not per-device)."""
    return {
        entity_id
        for (domain, _platform, uid), entity_id in rig.registry.entries.items()
        if not re.search(r"_(switch|gen_switch|light|rgb|climate|cover|tank|generator|gen_battery|gen_temp|gen_quiet|hourmeter|leveler_position|tank_alert)_", uid)
    }


def test_real_metadata_frames_build_identities() -> None:
    """Drive the real frame handler: SuccessMulti rows, then SuccessComplete."""
    from collections import defaultdict

    from custom_components.ha_onecontrol.protocol import events as ev

    c = make_coordinator()
    c._last_event_time = 0.0
    c._pending_get_devices_cmdids = {}
    c._pending_metadata_cmdids = {0x0102: 0xDC}
    c._pending_metadata_entries = {}
    c._metadata_requested_tables = {0xDC}
    c._cmd_correlation_stats = defaultdict(int)
    c._resolve_bootstrap_waiter = lambda *args: None
    c.hass = SimpleNamespace(async_create_task=lambda coro: coro.close())
    c.gateway_info = GatewayInformation(table_id=0xDC, device_metadata_table_crc=0xCAFEF00D)
    notices: list[object] = []
    c._event_callbacks.append(notices.append)

    def row(function_name: int, instance: int) -> bytes:
        payload = function_name.to_bytes(2, "big") + bytes([instance]) + bytes(14)
        assert len(payload) == ev.METADATA_PAYLOAD_SIZE_FULL
        return bytes([ev.METADATA_PROTOCOL_IDS_CAN, len(payload)]) + payload

    # Devices 0x08 (Tank Heater, 270) and 0x09 (Water Pump, 5).
    multi = bytes([0x02, 0x02, 0x01, 0x01, 0xDC, 0x08, 2]) + row(270, 0) + row(5, 0)
    c._process_frame(multi)
    assert c._identity_tables == set(), "nothing is committed until the completion frame"

    complete = bytes([0x02, 0x02, 0x01, 0x81]) + (0xCAFEF00D).to_bytes(4, "big") + bytes([2])
    c._process_frame(complete)

    assert c._identity_by_key == {"dc:08": "fn010e_00", "dc:09": "fn0005_00"}
    assert c.identity_address("fn0005_00") == (0xDC, 0x09)
    assert notices == [MetadataCommitted(table_id=0xDC)]
    assert c.device_entity_ready(0xDC, 0x09) is True
    assert c.entity_identity(0xDC, 0x09) == "fn0005_00"


# ── Bonded adapter diagnostic sensor ──────────────────────────────────


def _bonded_sensors(rig: "Rig") -> list:
    return [
        entity
        for uid, entity in rig.platforms["sensor"].loaded.items()
        if uid.endswith("_bonded_adapter")
    ]


def test_bonded_adapter_sensor_only_for_gateways_that_pair(platforms) -> None:
    rig = Rig(platforms)

    rig.coordinator.async_update_listeners()

    assert _bonded_sensors(rig) == []


def test_bonded_adapter_sensor_appears_once_a_bond_is_known(platforms) -> None:
    rig = Rig(platforms)
    coordinator = rig.coordinator
    coordinator.bonded_adapters = {"11:11:11:11:11:11": "hci0"}
    coordinator.connection_source = "11:11:11:11:11:11"
    coordinator._pinned_sources = frozenset({"111111111111"})

    coordinator.async_update_listeners()
    coordinator.async_update_listeners()

    (sensor,) = _bonded_sensors(rig)
    assert sensor.native_value == "11:11:11:11:11:11"
    assert sensor.available is True
    attributes = sensor.extra_state_attributes
    assert attributes["adapters"] == {"11:11:11:11:11:11": "hci0"}
    assert attributes["connected_through"] == "11:11:11:11:11:11"
    assert attributes["fallback_source"] is None
