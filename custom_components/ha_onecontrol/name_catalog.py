"""External naming catalog built from OneControl snapshot/manifest JSON files."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


IdentityKey = tuple[int, int, int, str]


@dataclass
class ExternalNameCatalog:
    """Lookup table for names keyed by stable device identity fields."""

    names_by_identity: dict[IdentityKey, str] = field(default_factory=dict)

    @property
    def entries(self) -> int:
        return len(self.names_by_identity)

    def lookup(
        self,
        device_type: int,
        device_instance: int,
        product_id: int,
        product_mac: str,
    ) -> str | None:
        key = (device_type, device_instance, product_id, _normalize_mac(product_mac))
        return self.names_by_identity.get(key)


def _normalize_mac(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value.upper() if ch in "0123456789ABCDEF")


def _extract_snapshot_name(description: str) -> str:
    """Prefer the leading friendly segment before hardware details in parentheses."""
    text = (description or "").strip()
    if not text:
        return ""
    idx = text.find(" (")
    return text[:idx].strip() if idx > 0 else text


def _extract_manifest_name(device: dict[str, Any]) -> str:
    function_name = str(device.get("FunctionName") or "").strip()
    if function_name and function_name.upper() != "UNKNOWN":
        return function_name

    base = str(device.get("Name") or "").strip() or "Device"
    instance = device.get("Instance")
    if isinstance(instance, int):
        return f"{base} {instance}"
    return base


def _coerce_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return int(value)
    raise ValueError("Expected integer value")


def _add_snapshot_entries(catalog: ExternalNameCatalog, data: dict[str, Any]) -> None:
    devices = data.get("DeviceSnapshot", {}).get("Devices", [])
    if not isinstance(devices, list):
        raise ValueError("Snapshot file has invalid DeviceSnapshot.Devices structure")

    for item in devices:
        if not isinstance(item, dict):
            continue
        logical = item.get("LogicalId")
        if not isinstance(logical, dict):
            continue

        name = _extract_snapshot_name(str(item.get("Description") or ""))
        if not name:
            continue

        try:
            key = (
                _coerce_int(logical.get("DeviceType")),
                _coerce_int(logical.get("DeviceInstance")),
                _coerce_int(logical.get("ProductId")),
                _normalize_mac(str(logical.get("ProductMacAddress") or "")),
            )
        except (TypeError, ValueError):
            continue

        if key[3]:
            # Snapshot has live runtime context and should win over manifest naming.
            catalog.names_by_identity[key] = name


def _add_manifest_entries(catalog: ExternalNameCatalog, data: dict[str, Any]) -> None:
    products = data.get("ProductList", [])
    if not isinstance(products, list):
        raise ValueError("Manifest file has invalid ProductList structure")

    for product in products:
        if not isinstance(product, dict):
            continue

        mac = _normalize_mac(str(product.get("UniqueID") or ""))
        product_id = product.get("TypeID")
        device_list = product.get("DeviceList", [])
        if not mac or not isinstance(product_id, int) or not isinstance(device_list, list):
            continue

        for device in device_list:
            if not isinstance(device, dict):
                continue
            try:
                key = (
                    _coerce_int(device.get("TypeID")),
                    _coerce_int(device.get("Instance")),
                    _coerce_int(product_id),
                    mac,
                )
            except (TypeError, ValueError):
                continue
            # Keep manifest only if a richer snapshot name has not been loaded.
            catalog.names_by_identity.setdefault(key, _extract_manifest_name(device))


def load_external_name_catalog(
    manifest_path: str | None,
    snapshot_path: str | None,
    manifest_json: str | None = None,
    snapshot_json: str | None = None,
) -> ExternalNameCatalog:
    """Load and merge naming catalogs from optional manifest/snapshot JSON sources."""
    catalog = ExternalNameCatalog()

    if manifest_json and manifest_json.strip():
        manifest_data = json.loads(manifest_json)
        _add_manifest_entries(catalog, manifest_data)
    elif manifest_path:
        manifest_text = Path(manifest_path).read_text(encoding="utf-8")
        manifest_data = json.loads(manifest_text)
        _add_manifest_entries(catalog, manifest_data)

    if snapshot_json and snapshot_json.strip():
        snapshot_data = json.loads(snapshot_json)
        _add_snapshot_entries(catalog, snapshot_data)
    elif snapshot_path:
        snapshot_text = Path(snapshot_path).read_text(encoding="utf-8")
        snapshot_data = json.loads(snapshot_text)
        _add_snapshot_entries(catalog, snapshot_data)

    return catalog
