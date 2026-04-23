"""Tests for external naming catalog and GetDevices identity parsing."""

from __future__ import annotations

import json

from custom_components.ha_onecontrol.name_catalog import load_external_name_catalog
from custom_components.ha_onecontrol.protocol.events import parse_get_devices_response


def test_parse_get_devices_identity_row() -> None:
    """GetDevices SuccessMulti row should decode identity fields correctly."""
    # [cmdL cmdH 0x02 0x01][table=0x03][start=0xE7][count=1]
    # entry: [protocol=2][payloadSize=10]
    # payload: [type=20][instance=4][productId=0x0067][mac=00 00 00 08 E9 BC]
    frame = bytes.fromhex(
        "34 12 02 01 03 E7 01 02 0A 14 04 00 67 00 00 00 08 E9 BC"
    )

    rows = parse_get_devices_response(frame)
    assert len(rows) == 1
    row = rows[0]
    assert row.table_id == 0x03
    assert row.device_id == 0xE7
    assert row.protocol == 2
    assert row.device_type == 20
    assert row.device_instance == 4
    assert row.product_id == 103
    assert row.product_mac == "00000008E9BC"


def test_external_name_catalog_manifest_and_snapshot(tmp_path) -> None:
    """Snapshot/manifest should build a lookup keyed by stable identity fields."""
    manifest = {
        "ProductList": [
            {
                "UniqueID": "00:00:00:08:E9:BC",
                "TypeID": 103,
                "DeviceList": [
                    {
                        "TypeID": 20,
                        "Instance": 4,
                        "FunctionName": "Kitchen Chandelier Light",
                    }
                ],
            }
        ]
    }
    snapshot = {
        "DeviceSnapshot": {
            "Devices": [
                {
                    "Description": "Kitchen Chandelier Light (Dimmable Light, Lighting Control)",
                    "LogicalId": {
                        "DeviceType": 20,
                        "DeviceInstance": 4,
                        "ProductId": 103,
                        "ProductMacAddress": "00000008E9BC",
                    },
                }
            ]
        }
    }

    manifest_path = tmp_path / "manifest.json"
    snapshot_path = tmp_path / "snapshot.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    catalog = load_external_name_catalog(str(manifest_path), str(snapshot_path))
    assert catalog.entries == 1
    assert catalog.lookup(20, 4, 103, "00:00:00:08:E9:BC") == "Kitchen Chandelier Light"


def test_external_name_catalog_from_raw_json_text() -> None:
    """Catalog should accept raw manifest/snapshot JSON text without file paths."""
    manifest = {
        "ProductList": [
            {
                "UniqueID": "00:00:00:08:E9:BC",
                "TypeID": 103,
                "DeviceList": [
                    {
                        "TypeID": 20,
                        "Instance": 4,
                        "FunctionName": "Kitchen Chandelier Light",
                    }
                ],
            }
        ]
    }
    snapshot = {
        "DeviceSnapshot": {
            "Devices": [
                {
                    "Description": "Kitchen Chandelier Light (Dimmable Light, Lighting Control)",
                    "LogicalId": {
                        "DeviceType": 20,
                        "DeviceInstance": 4,
                        "ProductId": 103,
                        "ProductMacAddress": "00000008E9BC",
                    },
                }
            ]
        }
    }

    catalog = load_external_name_catalog(
        None,
        None,
        json.dumps(manifest),
        json.dumps(snapshot),
    )

    assert catalog.entries == 1
    assert catalog.lookup(20, 4, 103, "00:00:00:08:E9:BC") == "Kitchen Chandelier Light"
