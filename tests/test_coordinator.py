from types import SimpleNamespace

from custom_components.ha_onecontrol.coordinator import OneControlCoordinator
from custom_components.ha_onecontrol.protocol.events import CoverStatus, RelayStatus


def test_dispatch_can_entity_processes_relay_status_for_device_type_30() -> None:
    coordinator = object.__new__(OneControlCoordinator)
    coordinator._can_device_types = {0x86: 30}
    coordinator._invalid_can_sources = set()
    coordinator.device_names = {}
    coordinator.tanks = {}
    coordinator.covers = {}
    coordinator.relays = {}
    coordinator._event_callbacks = []
    coordinator._last_event_time = 0.0
    coordinator.device_name = lambda table_id, device_id: "Exterior Light"
    coordinator.async_set_updated_data = lambda data: None
    coordinator._build_data = lambda: {}

    wire = SimpleNamespace(message_type=0x03, source_address=0x86, payload=b"\x81\xff\x01\x00\x00\x00")

    callback_results = []

    def event_callback(event: object) -> None:
        callback_results.append(event)

    coordinator._event_callbacks.append(event_callback)

    coordinator._dispatch_can_entity(wire, None)

    assert "00:86" in coordinator.relays
    event = coordinator.relays["00:86"]
    assert isinstance(event, RelayStatus)
    assert event.is_on is True
    assert callback_results == [event]


def test_dispatch_can_entity_reinterprets_type_30_cover_from_device_name() -> None:
    coordinator = object.__new__(OneControlCoordinator)
    coordinator._can_device_types = {0x86: 30}
    coordinator._invalid_can_sources = set()
    coordinator.device_names = {}
    coordinator.tanks = {}
    coordinator.covers = {}
    coordinator.relays = {}
    coordinator._event_callbacks = []
    coordinator._last_event_time = 0.0
    coordinator.device_name = lambda table_id, device_id: "Power Awning"
    coordinator.async_set_updated_data = lambda data: None
    coordinator._build_data = lambda: {}

    wire = SimpleNamespace(message_type=0x03, source_address=0x86, payload=b"\xC2\x32")

    callback_results = []

    def event_callback(event: object) -> None:
        callback_results.append(event)

    coordinator._event_callbacks.append(event_callback)

    coordinator._dispatch_can_entity(wire, None)

    assert "00:86" in coordinator.covers
    event = coordinator.covers["00:86"]
    assert event.status == 0xC2
    assert event.position == 0x32
    assert callback_results == [event]


def test_dispatch_can_entity_processes_hbridge_type_5_cover_state() -> None:
    coordinator = object.__new__(OneControlCoordinator)
    coordinator._can_device_types = {0x86: 5}
    coordinator._invalid_can_sources = set()
    coordinator.device_names = {}
    coordinator.tanks = {}
    coordinator.covers = {}
    coordinator.relays = {}
    coordinator._event_callbacks = []
    coordinator._last_event_time = 0.0
    coordinator.device_name = lambda table_id, device_id: "Bedroom Slide"
    coordinator.async_set_updated_data = lambda data: None
    coordinator._build_data = lambda: {}

    wire = SimpleNamespace(message_type=0x03, source_address=0x86, payload=b"\xC3\x64")

    callback_results = []

    def event_callback(event: object) -> None:
        callback_results.append(event)

    coordinator._event_callbacks.append(event_callback)

    coordinator._dispatch_can_entity(wire, None)

    assert "00:86" in coordinator.covers
    event = coordinator.covers["00:86"]
    assert isinstance(event, CoverStatus)
    assert event.status == 0xC3
    assert event.position == 0x64
    assert callback_results == [event]


def test_dispatch_can_entity_processes_hbridge_type_32_cover_state() -> None:
    coordinator = object.__new__(OneControlCoordinator)
    coordinator._can_device_types = {0x86: 32}
    coordinator._invalid_can_sources = set()
    coordinator.device_names = {}
    coordinator.tanks = {}
    coordinator.covers = {}
    coordinator.relays = {}
    coordinator._event_callbacks = []
    coordinator._last_event_time = 0.0
    coordinator.device_name = lambda table_id, device_id: "Slide Awning"
    coordinator.async_set_updated_data = lambda data: None
    coordinator._build_data = lambda: {}

    wire = SimpleNamespace(message_type=0x03, source_address=0x86, payload=b"\xC2\x20")

    callback_results = []

    def event_callback(event: object) -> None:
        callback_results.append(event)

    coordinator._event_callbacks.append(event_callback)

    coordinator._dispatch_can_entity(wire, None)

    assert "00:86" in coordinator.covers
    event = coordinator.covers["00:86"]
    assert isinstance(event, CoverStatus)
    assert event.status == 0xC2
    assert event.position == 0x20
    assert callback_results == [event]


def test_dispatch_can_entity_recognizes_vent_cover_device_name() -> None:
    coordinator = object.__new__(OneControlCoordinator)
    # Unrecognized device type -> cover classification comes from the name.
    coordinator._can_device_types = {0x86: 1}
    coordinator._invalid_can_sources = set()
    coordinator.device_names = {}
    coordinator.tanks = {}
    coordinator.covers = {}
    coordinator.relays = {}
    coordinator._event_callbacks = []
    coordinator._last_event_time = 0.0
    coordinator.device_name = lambda table_id, device_id: "Kitchen Vent Cover"
    coordinator.async_set_updated_data = lambda data: None
    coordinator._build_data = lambda: {}

    wire = SimpleNamespace(message_type=0x03, source_address=0x86, payload=b"\xC0\xff")

    callback_results = []

    def event_callback(event: object) -> None:
        callback_results.append(event)

    coordinator._event_callbacks.append(event_callback)

    coordinator._dispatch_can_entity(wire, None)

    assert "00:86" in coordinator.covers
    event = coordinator.covers["00:86"]
    assert isinstance(event, CoverStatus)
    assert event.status == 0xC0
    assert event.position is None
    assert callback_results == [event]
