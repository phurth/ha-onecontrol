"""Tests for adapter-scoped BlueZ lookups (multiple local adapters)."""

from types import SimpleNamespace

from custom_components.ha_onecontrol.ble_agent import (
    _adapter_path_in,
    _bonded_adapter_macs_in,
    _device_path_in,
)
from custom_components.ha_onecontrol.coordinator import _pick_local_candidate

GATEWAY = "AA:BB:CC:DD:EE:FF"
HCI0_MAC = "11:11:11:11:11:11"
HCI1_MAC = "22:22:22:22:22:22"
HCI0_DEV = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
HCI1_DEV = "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"


def _variant(value: object) -> SimpleNamespace:
    """Stand-in for a dbus_fast Variant."""
    return SimpleNamespace(value=value)


def _objects(paired_on: tuple[str, ...] = ()) -> dict:
    """A GetManagedObjects result with the gateway seen by both adapters."""
    return {
        "/org/bluez": {"org.bluez.AgentManager1": {}},
        "/org/bluez/hci0": {"org.bluez.Adapter1": {"Address": _variant(HCI0_MAC)}},
        "/org/bluez/hci1": {"org.bluez.Adapter1": {"Address": _variant(HCI1_MAC)}},
        HCI0_DEV: {"org.bluez.Device1": {"Paired": _variant("hci0" in paired_on)}},
        HCI1_DEV: {"org.bluez.Device1": {"Paired": _variant("hci1" in paired_on)}},
        HCI1_DEV + "/service0010": {"org.bluez.GattService1": {}},
    }


def test_adapter_path_resolves_by_name_or_mac() -> None:
    objects = _objects()
    assert _adapter_path_in(objects, "hci1") == "/org/bluez/hci1"
    assert _adapter_path_in(objects, HCI1_MAC) == "/org/bluez/hci1"
    assert _adapter_path_in(objects, HCI1_MAC.lower()) == "/org/bluez/hci1"
    assert _adapter_path_in(objects, "hci7") is None


def test_device_path_is_scoped_to_the_requested_adapter() -> None:
    objects = _objects()
    assert _device_path_in(objects, GATEWAY, HCI1_MAC) == HCI1_DEV
    assert _device_path_in(objects, GATEWAY, "hci0") == HCI0_DEV
    # Unscoped lookups keep returning the first match.
    assert _device_path_in(objects, GATEWAY) == HCI0_DEV


def test_device_path_never_falls_back_to_another_adapter() -> None:
    objects = _objects()
    del objects[HCI1_DEV]
    assert _device_path_in(objects, GATEWAY, HCI1_MAC) is None
    assert _device_path_in(objects, GATEWAY, "hci7") is None


def test_bonded_adapter_macs_reports_only_paired_adapters() -> None:
    assert _bonded_adapter_macs_in(_objects(), GATEWAY) == set()
    assert _bonded_adapter_macs_in(_objects(("hci1",)), GATEWAY) == {HCI1_MAC}
    assert _bonded_adapter_macs_in(_objects(("hci0", "hci1")), GATEWAY) == {
        HCI0_MAC,
        HCI1_MAC,
    }


def _candidate(source: str, rssi: int) -> SimpleNamespace:
    return SimpleNamespace(
        scanner=SimpleNamespace(source=source),
        advertisement=SimpleNamespace(rssi=rssi),
    )


def test_pick_local_candidate_prefers_the_bonded_adapter() -> None:
    weak_bonded = _candidate(HCI1_MAC, -80)
    candidates = [_candidate("proxy-east", -40), _candidate(HCI0_MAC, -60), weak_bonded]
    picked = _pick_local_candidate(candidates, {HCI0_MAC, HCI1_MAC}, {HCI1_MAC})
    assert picked is weak_bonded


def test_pick_local_candidate_uses_strongest_signal_when_unbonded() -> None:
    strong = _candidate(HCI1_MAC, -62)
    candidates = [_candidate(HCI0_MAC, -78), strong, _candidate("proxy-east", -40)]
    assert _pick_local_candidate(candidates, {HCI0_MAC, HCI1_MAC}, set()) is strong


def test_pick_local_candidate_ignores_proxies() -> None:
    assert _pick_local_candidate([_candidate("proxy-east", -40)], {HCI0_MAC}, set()) is None


def test_pick_local_candidate_sticks_to_the_stored_source_when_unbonded() -> None:
    stored = _candidate(HCI0_MAC, -78)
    candidates = [stored, _candidate(HCI1_MAC, -62)]
    picked = _pick_local_candidate(candidates, {HCI0_MAC, HCI1_MAC}, set(), HCI0_MAC)
    assert picked is stored


def test_pick_local_candidate_bond_outranks_the_stored_source() -> None:
    bonded = _candidate(HCI1_MAC, -80)
    candidates = [_candidate(HCI0_MAC, -60), bonded]
    picked = _pick_local_candidate(
        candidates, {HCI0_MAC, HCI1_MAC}, {HCI1_MAC}, HCI0_MAC
    )
    assert picked is bonded
