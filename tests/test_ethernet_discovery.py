"""Tests for IDS CAN-to-Ethernet UDP discovery helpers."""

import asyncio

from custom_components.ha_onecontrol.const import DEFAULT_ETH_PORT
from custom_components.ha_onecontrol.protocol.ethernet_discovery import (
    _is_supported_bridge,
    _parse_port,
    discover_can_ethernet_bridges,
)


def test_is_supported_bridge_by_mfg() -> None:
    payload = {"Mfg": "IDS", "Product": "OTHER"}
    assert _is_supported_bridge(payload)


def test_is_supported_bridge_by_product() -> None:
    payload = {"Mfg": "unknown", "Product": "CAN_TO_ETHERNET_GATEWAY"}
    assert _is_supported_bridge(payload)


def test_is_supported_bridge_lowercase_keys() -> None:
    payload = {"mfg": "IDS", "product": "CAN_TO_ETHERNET_GATEWAY"}
    assert _is_supported_bridge(payload)


def test_is_supported_bridge_negative() -> None:
    payload = {"Mfg": "other", "Product": "other"}
    assert not _is_supported_bridge(payload)


def test_parse_port_from_int() -> None:
    assert _parse_port({"Port": 6969}) == 6969


def test_parse_port_from_string() -> None:
    assert _parse_port({"Port": "6970"}) == 6970


def test_parse_port_lowercase_key() -> None:
    assert _parse_port({"port": "6969"}) == 6969


def test_parse_port_invalid_fallback() -> None:
    assert _parse_port({"Port": "not-a-number"}) == DEFAULT_ETH_PORT


def test_parse_port_bool_fallback() -> None:
    assert _parse_port({"Port": True}) == DEFAULT_ETH_PORT


def test_discover_timeout_empty() -> None:
    discovered = asyncio.run(discover_can_ethernet_bridges(timeout_s=0.0))
    assert discovered == []
