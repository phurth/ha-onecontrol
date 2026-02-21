"""Event parsers for decoded COBS frames from OneControl gateways.

Each decoded frame has the event-type byte at index 0.
These helpers return typed dataclass instances or ``None`` on parse failure.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

from ..const import (
    EVENT_DEVICE_ONLINE_STATUS,
    EVENT_DIMMABLE_LIGHT,
    EVENT_GATEWAY_INFORMATION,
    EVENT_HBRIDGE_1,
    EVENT_HBRIDGE_2,
    EVENT_HVAC_STATUS,
    EVENT_RELAY_BASIC_LATCHING_1,
    EVENT_RELAY_BASIC_LATCHING_2,
    EVENT_RV_STATUS,
    EVENT_TANK_SENSOR,
    EVENT_TANK_SENSOR_V2,
)


# ── Dataclasses ───────────────────────────────────────────────────────────


@dataclass
class GatewayInformation:
    protocol_version: int = 0
    options: int = 0
    device_count: int = 0
    table_id: int = 0


@dataclass
class RvStatus:
    """System voltage and temperature (event 0x07)."""

    voltage: float | None = None  # Volts (8.8 fixed-point BE)
    temperature: float | None = None  # °F (8.8 fixed-point BE, signed)


@dataclass
class RelayStatus:
    table_id: int = 0
    device_id: int = 0
    is_on: bool = False
    dtc_code: int | None = None


@dataclass
class DeviceOnline:
    table_id: int = 0
    device_id: int = 0
    is_online: bool = False


@dataclass
class TankLevel:
    table_id: int = 0
    device_id: int = 0
    level: int = 0  # 0-100 %


@dataclass
class DimmableLight:
    table_id: int = 0
    device_id: int = 0
    brightness: int = 0
    mode: int = 0  # 0=Off,1=On,2=Blink,3=Swell

    @property
    def is_on(self) -> bool:
        return self.mode > 0


@dataclass
class HvacZone:
    table_id: int = 0
    device_id: int = 0
    heat_mode: int = 0  # 0=Off,1=Heat,2=Cool,3=Both
    heat_source: int = 0  # 0=Gas,1=HeatPump
    fan_mode: int = 0  # 0=Auto,1=High,2=Low
    low_trip_f: int = 0  # Heating setpoint °F
    high_trip_f: int = 0  # Cooling setpoint °F
    zone_status: int = 0
    indoor_temp_f: float | None = None
    outdoor_temp_f: float | None = None


@dataclass
class CoverStatus:
    table_id: int = 0
    device_id: int = 0
    status: int = 0  # 0xC0=stopped, 0xC2=opening, 0xC3=closing
    position: int | None = None  # 0-100 or None


# ── Parsers ───────────────────────────────────────────────────────────────


def parse_gateway_information(data: bytes) -> GatewayInformation | None:
    if len(data) < 5:
        return None
    return GatewayInformation(
        protocol_version=data[1],
        options=data[2],
        device_count=data[3],
        table_id=data[4],
    )


def parse_rv_status(data: bytes) -> RvStatus | None:
    """Parse RvStatus (0x07).  Format: [0x07][voltH][voltL][tempH][tempL][flags]."""
    if len(data) < 6:
        return None

    v_raw = (data[1] << 8) | data[2]
    t_raw = (data[3] << 8) | data[4]

    voltage = None if v_raw == 0xFFFF else v_raw / 256.0
    # 0x7FFF and 0xFFFF are "unavailable" sentinels
    if t_raw in (0xFFFF, 0x7FFF):
        temperature = None
    else:
        temperature = t_raw / 256.0

    return RvStatus(voltage=voltage, temperature=temperature)


def parse_relay_status(data: bytes) -> RelayStatus | None:
    if len(data) < 5:
        return None
    status_byte = data[3] & 0xFF
    is_on = (status_byte & 0x0F) == 0x01
    dtc = None
    if len(data) >= 9:
        dtc = (data[5] << 8) | data[6]
    return RelayStatus(
        table_id=data[1],
        device_id=data[2],
        is_on=is_on,
        dtc_code=dtc if dtc else None,
    )


def parse_device_online(data: bytes) -> DeviceOnline | None:
    if len(data) < 4:
        return None
    return DeviceOnline(
        table_id=data[1],
        device_id=data[2],
        is_online=data[3] != 0,
    )


def parse_tank_status(data: bytes) -> list[TankLevel]:
    """Parse TankSensorStatus (0x0C) — may contain multiple tanks."""
    if len(data) < 4:
        return []
    table_id = data[1]
    tanks: list[TankLevel] = []
    idx = 2
    while idx + 1 < len(data):
        tanks.append(TankLevel(table_id=table_id, device_id=data[idx], level=data[idx + 1]))
        idx += 2
    return tanks


def parse_tank_status_v2(data: bytes) -> TankLevel | None:
    """Parse TankSensorStatusV2 (0x1B) — single tank per event."""
    if len(data) < 4:
        return None
    return TankLevel(table_id=data[1], device_id=data[2], level=data[3])


def parse_dimmable_light(data: bytes) -> DimmableLight | None:
    if len(data) < 5:
        return None
    mode = data[3]
    brightness = data[6] if len(data) >= 7 else data[4]
    return DimmableLight(
        table_id=data[1],
        device_id=data[2],
        brightness=brightness,
        mode=mode,
    )


def _decode_temp_88(raw: int) -> float | None:
    """Decode a signed 8.8 fixed-point temperature value."""
    if raw in (0x8000, 0x2FF0, 0xFFFF):
        return None
    signed = raw - 0x10000 if raw >= 0x8000 else raw
    return signed / 256.0


def parse_hvac_status(data: bytes) -> list[HvacZone]:
    """Parse HvacStatus (0x0B) — may contain multiple zones (11 bytes each)."""
    if len(data) < 4:
        return []
    table_id = data[1]
    BYTES_PER_DEVICE = 11
    zones: list[HvacZone] = []
    offset = 2
    while offset + BYTES_PER_DEVICE <= len(data):
        device_id = data[offset]
        cmd = data[offset + 1]
        low_f = data[offset + 2]
        high_f = data[offset + 3]
        status = data[offset + 4] & 0x8F
        indoor_raw = (data[offset + 5] << 8) | data[offset + 6]
        outdoor_raw = (data[offset + 7] << 8) | data[offset + 8]

        zones.append(
            HvacZone(
                table_id=table_id,
                device_id=device_id,
                heat_mode=cmd & 0x07,
                heat_source=(cmd >> 4) & 0x03,
                fan_mode=(cmd >> 6) & 0x03,
                low_trip_f=low_f,
                high_trip_f=high_f,
                zone_status=status,
                indoor_temp_f=_decode_temp_88(indoor_raw),
                outdoor_temp_f=_decode_temp_88(outdoor_raw),
            )
        )
        offset += BYTES_PER_DEVICE
    return zones


def parse_cover_status(data: bytes) -> CoverStatus | None:
    if len(data) < 4:
        return None
    pos = data[4] if len(data) > 4 else None
    if pos is not None and pos == 0xFF:
        pos = None
    return CoverStatus(
        table_id=data[1],
        device_id=data[2],
        status=data[3],
        position=pos,
    )


def parse_event(data: bytes):
    """Dispatch a decoded COBS frame to the appropriate parser.

    Returns a parsed dataclass or the raw bytes if the event type is
    unrecognised.
    """
    if not data:
        return None
    event_type = data[0]

    if event_type == EVENT_GATEWAY_INFORMATION:
        return parse_gateway_information(data)
    if event_type == EVENT_RV_STATUS:
        return parse_rv_status(data)
    if event_type in (EVENT_RELAY_BASIC_LATCHING_1, EVENT_RELAY_BASIC_LATCHING_2):
        return parse_relay_status(data)
    if event_type == EVENT_DEVICE_ONLINE_STATUS:
        return parse_device_online(data)
    if event_type == EVENT_TANK_SENSOR:
        return parse_tank_status(data)
    if event_type == EVENT_TANK_SENSOR_V2:
        return parse_tank_status_v2(data)
    if event_type == EVENT_DIMMABLE_LIGHT:
        return parse_dimmable_light(data)
    if event_type == EVENT_HVAC_STATUS:
        return parse_hvac_status(data)
    if event_type in (EVENT_HBRIDGE_1, EVENT_HBRIDGE_2):
        return parse_cover_status(data)

    # Return raw bytes for unknown events
    return data
