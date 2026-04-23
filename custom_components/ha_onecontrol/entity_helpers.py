"""Shared entity helpers for transport-specific DeviceInfo fields."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONNECTION_TYPE_ETHERNET, DOMAIN


def build_gateway_device_info(address: str, connection_type: str) -> DeviceInfo:
    """Build DeviceInfo with transport-appropriate connection metadata."""
    model = "Ethernet Gateway" if connection_type == CONNECTION_TYPE_ETHERNET else "BLE Gateway"
    base = {
        "identifiers": {(DOMAIN, address)},
        "name": f"OneControl {address}",
        "manufacturer": "Lippert / LCI",
        "model": model,
    }
    if connection_type != CONNECTION_TYPE_ETHERNET:
        return DeviceInfo(
            **base,
            connections={("bluetooth", address)},
        )
    return DeviceInfo(**base)
