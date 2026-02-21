"""Event parsers for decoded COBS frames from OneControl gateways.

Each decoded frame has the event-type byte at index 0.
These helpers return typed dataclass instances or ``None`` on parse failure.

Reference: INTERNALS.md § Event Types, Event Parsing Examples
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..const import (
    EVENT_DEVICE_COMMAND,
    EVENT_DEVICE_LOCK_STATUS,
    EVENT_DEVICE_ONLINE_STATUS,
    EVENT_DIMMABLE_LIGHT,
    EVENT_GATEWAY_INFORMATION,
    EVENT_GENERATOR_GENIE,
    EVENT_HBRIDGE_1,
    EVENT_HBRIDGE_2,
    EVENT_HOUR_METER,
    EVENT_HVAC_STATUS,
    EVENT_REAL_TIME_CLOCK,
    EVENT_RELAY_BASIC_LATCHING_1,
    EVENT_RELAY_BASIC_LATCHING_2,
    EVENT_RGB_LIGHT,
    EVENT_RV_STATUS,
    EVENT_SESSION_STATUS,
    EVENT_TANK_SENSOR,
    EVENT_TANK_SENSOR_V2,
    METADATA_PAYLOAD_SIZE_FULL,
    METADATA_PROTOCOL_HOST,
    METADATA_PROTOCOL_IDS_CAN,
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
    feature_flags: int = 0  # data[5]


@dataclass
class RelayStatus:
    """Relay Basic/Latching status (event 0x05/0x06).

    INTERNALS.md § Relay Status:
      Standard 5-byte: [evt][tbl][dev][status][res]
      Extended 9-byte: bytes 5-6 = DTC code (BE)
    """

    table_id: int = 0
    device_id: int = 0
    is_on: bool = False
    status_byte: int = 0
    dtc_code: int = 0  # 0 = no fault


@dataclass
class DeviceOnline:
    table_id: int = 0
    device_id: int = 0
    is_online: bool = False


@dataclass
class SystemLockout:
    """System-wide in-motion lockout level from DeviceLockStatus (0x04).

    Preferred bitfield format (>=8 bytes):
      [0x04][lockoutLevel][...][tableId][deviceCount][bitfield...]
    lockout_level > 0 means the RV is in motion and devices are locked.
    """

    lockout_level: int = 0
    table_id: int = 0
    device_count: int = 0
    per_device_locked: dict[int, bool] | None = None  # device_id → locked


@dataclass
class DeviceLock:
    """Device lock status (event 0x04) — legacy format."""

    table_id: int = 0
    device_id: int = 0
    is_locked: bool = False


@dataclass
class TankLevel:
    table_id: int = 0
    device_id: int = 0
    level: int = 0  # 0-100 %


@dataclass
class DimmableLight:
    """Dimmable light status (event 0x08).

    INTERNALS.md § Dimmable Light:
      11-byte: brightness at data[6] (statusBytes[3])
      5-byte legacy: brightness at data[4]
    """

    table_id: int = 0
    device_id: int = 0
    brightness: int = 0
    mode: int = 0  # 0=Off,1=On,2=Blink,3=Swell

    @property
    def is_on(self) -> bool:
        return self.mode > 0


@dataclass
class RgbLight:
    """RGB light status (event 0x09)."""

    table_id: int = 0
    device_id: int = 0
    mode: int = 0  # 0=Off,1=Solid,2=Blink,4-8=Transitions,127=Restore
    red: int = 0
    green: int = 0
    blue: int = 0
    brightness: int = 255

    @property
    def is_on(self) -> bool:
        return self.mode > 0


@dataclass
class HvacZone:
    """HVAC zone status (event 0x0B).

    INTERNALS.md § HVAC Command:
      bits 0-2 = heat_mode, bits 4-5 = heat_source, bits 6-7 = fan_mode
    """

    table_id: int = 0
    device_id: int = 0
    heat_mode: int = 0  # 0=Off,1=Heat,2=Cool,3=Both,4=Schedule
    heat_source: int = 0  # 0=Gas,1=HeatPump
    fan_mode: int = 0  # 0=Auto,1=High,2=Low
    low_trip_f: int = 0  # Heating setpoint °F
    high_trip_f: int = 0  # Cooling setpoint °F
    zone_status: int = 0
    indoor_temp_f: float | None = None
    outdoor_temp_f: float | None = None
    dtc_code: int = 0


@dataclass
class CoverStatus:
    """H-Bridge / cover status (event 0x0D/0x0E).

    INTERNALS.md § Cover/Slide/Awning:
      STATE-ONLY.  No commands (safety: no limit switches, 19-39A motors).
      0xC0=stopped, 0xC2=opening, 0xC3=closing.
    """

    table_id: int = 0
    device_id: int = 0
    status: int = 0
    position: int | None = None  # 0-100 or None if 0xFF

    @property
    def ha_state(self) -> str:
        """HA-compatible state string."""
        return {0xC2: "opening", 0xC3: "closing", 0xC0: "stopped"}.get(
            self.status, "unknown"
        )


@dataclass
class RealTimeClock:
    """Gateway real-time clock (event 0x20)."""

    year: int = 0
    month: int = 0
    day: int = 0
    hour: int = 0
    minute: int = 0
    second: int = 0
    weekday: int = 0


@dataclass
class HourMeter:
    """Generator / device hour meter (event 0x0F)."""

    table_id: int = 0
    device_id: int = 0
    hours: float = 0.0


@dataclass
class GeneratorStatus:
    """Generator Genie status (event 0x0A)."""

    table_id: int = 0
    device_id: int = 0
    is_running: bool = False


@dataclass
class DeviceMetadata:
    """Parsed metadata from GetDevicesMetadata (event 0x02) response.

    INTERNALS.md § Device Metadata Retrieval:
      function_name is BIG-ENDIAN (contrary to rest of protocol)
      Protocol 1 (Host) with payloadSize=17 uses same fields as Protocol 2 (IdsCan)
    """

    table_id: int = 0
    device_id: int = 0
    function_name: int = 0
    function_instance: int = 0


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
    """Parse RvStatus (0x07).

    Format: [0x07][voltH][voltL][tempH][tempL][flags]
    Both voltage and temperature are unsigned 8.8 fixed-point big-endian.
    """
    if len(data) < 6:
        return None

    v_raw = (data[1] << 8) | data[2]
    t_raw = (data[3] << 8) | data[4]

    voltage = None if v_raw == 0xFFFF else v_raw / 256.0
    if t_raw in (0xFFFF, 0x7FFF):
        temperature = None
    else:
        temperature = t_raw / 256.0

    return RvStatus(voltage=voltage, temperature=temperature, feature_flags=data[5])


def parse_relay_status(data: bytes) -> RelayStatus | None:
    """Parse relay status (0x05/0x06).

    INTERNALS.md § Relay Status:
      Standard 5-byte, extended 9-byte with DTC at bytes 5-6 (BE).
      Status low nibble: 0x01=ON, 0x00=OFF.
    """
    if len(data) < 5:
        return None
    status_byte = data[3] & 0xFF
    is_on = (status_byte & 0x0F) == 0x01
    dtc = 0
    if len(data) >= 9:
        dtc = (data[5] << 8) | data[6]
    return RelayStatus(
        table_id=data[1],
        device_id=data[2],
        is_on=is_on,
        status_byte=status_byte,
        dtc_code=dtc,
    )


def parse_device_online(data: bytes) -> DeviceOnline | None:
    """Parse DeviceOnlineStatus (0x03).  data[3] != 0 → online."""
    if len(data) < 4:
        return None
    return DeviceOnline(
        table_id=data[1],
        device_id=data[2],
        is_online=data[3] != 0xFF,  # 0xFF = offline per Android source
    )


def parse_device_lock(data: bytes) -> SystemLockout | DeviceLock | None:
    """Parse DeviceLockStatus (0x04).

    Two formats (from Android handleDeviceLockStatus):
      Bitfield (>=8 bytes): [0x04][lockoutLevel][??][??][??][??][tableId][deviceCount][bitfield...]
      Legacy   (<8 bytes): [0x04][tableId][deviceId][status]
    """
    if len(data) < 4:
        return None

    # Bitfield format — system-wide lockout
    if len(data) >= 8:
        lockout_level = data[1] & 0xFF
        table_id = data[6] & 0xFF
        device_count = data[7] & 0xFF
        lock_byte_count = (device_count + 7) // 8
        per_device: dict[int, bool] | None = None
        if len(data) >= 8 + lock_byte_count:
            per_device = {}
            for idx in range(device_count):
                status_byte = data[8 + (idx // 8)] & 0xFF
                bit_mask = 1 << (idx % 8)
                per_device[idx] = (status_byte & bit_mask) != 0
        return SystemLockout(
            lockout_level=lockout_level,
            table_id=table_id,
            device_count=device_count,
            per_device_locked=per_device,
        )

    # Legacy format — single device
    return DeviceLock(
        table_id=data[1],
        device_id=data[2],
        is_locked=data[3] != 0,
    )


def parse_tank_status(data: bytes) -> list[TankLevel]:
    """Parse TankSensorStatus (0x0C) — multi-tank batched format.

    INTERNALS.md § Tank Sensors:
      Format: [0x0C][tableId][deviceId1][level1][deviceId2][level2]...
      Each tank = 2 bytes. Number of tanks = (len - 2) / 2.
    """
    if len(data) < 4:
        return []
    table_id = data[1]
    tanks: list[TankLevel] = []
    idx = 2
    while idx + 1 < len(data):
        tanks.append(
            TankLevel(table_id=table_id, device_id=data[idx], level=data[idx + 1])
        )
        idx += 2
    return tanks


def parse_tank_status_v2(data: bytes) -> TankLevel | None:
    """Parse TankSensorStatusV2 (0x1B) — single tank per event."""
    if len(data) < 4:
        return None
    return TankLevel(table_id=data[1], device_id=data[2], level=data[3])


def parse_dimmable_light(data: bytes) -> DimmableLight | None:
    """Parse DimmableLightStatus (0x08).

    INTERNALS.md § Dimmable Light:
      11-byte frame: brightness at data[6] (statusBytes[3])
      5-byte legacy:  brightness at data[4]
    """
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


def parse_rgb_light(data: bytes) -> RgbLight | None:
    """Parse RgbLightStatus (0x09)."""
    if len(data) < 5:
        return None
    mode = data[3]
    # Extended format has RGB at offsets 4,5,6
    r = data[4] if len(data) > 4 else 0
    g = data[5] if len(data) > 5 else 0
    b = data[6] if len(data) > 6 else 0
    bright = data[7] if len(data) > 7 else 255
    return RgbLight(
        table_id=data[1],
        device_id=data[2],
        mode=mode,
        red=r,
        green=g,
        blue=b,
        brightness=bright,
    )


def parse_generator_status(data: bytes) -> GeneratorStatus | None:
    """Parse GeneratorGenie status (0x0A)."""
    if len(data) < 4:
        return None
    return GeneratorStatus(
        table_id=data[1],
        device_id=data[2],
        is_running=data[3] != 0,
    )


def _decode_temp_88(raw: int) -> float | None:
    """Decode a signed 8.8 fixed-point temperature value.

    Sentinels: 0x8000, 0x2FF0 → invalid / unavailable.
    """
    if raw in (0x8000, 0x2FF0, 0xFFFF):
        return None
    signed = raw - 0x10000 if raw >= 0x8000 else raw
    return signed / 256.0


def parse_hvac_status(data: bytes) -> list[HvacZone]:
    """Parse HvacStatus (0x0B) — multiple zones, 11 bytes each.

    INTERNALS.md § HVAC Status Event:
      [0x0B][tableId] then per zone (11B):
        [devId][cmdByte][lowTrip][highTrip][zoneStatus]
        [indoorH][indoorL][outdoorH][outdoorL][dtcH][dtcL]
    """
    if len(data) < 4:
        return []
    table_id = data[1]
    BYTES_PER_ZONE = 11
    zones: list[HvacZone] = []
    offset = 2
    while offset + BYTES_PER_ZONE <= len(data):
        device_id = data[offset]
        cmd = data[offset + 1]
        low_f = data[offset + 2]
        high_f = data[offset + 3]
        status = data[offset + 4]
        indoor_raw = (data[offset + 5] << 8) | data[offset + 6]
        outdoor_raw = (data[offset + 7] << 8) | data[offset + 8]
        dtc = (data[offset + 9] << 8) | data[offset + 10]

        zones.append(
            HvacZone(
                table_id=table_id,
                device_id=device_id,
                heat_mode=cmd & 0x07,
                heat_source=(cmd >> 4) & 0x03,
                fan_mode=(cmd >> 6) & 0x03,
                low_trip_f=low_f,
                high_trip_f=high_f,
                zone_status=status & 0x8F,
                indoor_temp_f=_decode_temp_88(indoor_raw),
                outdoor_temp_f=_decode_temp_88(outdoor_raw),
                dtc_code=dtc,
            )
        )
        offset += BYTES_PER_ZONE
    return zones


def parse_cover_status(data: bytes) -> CoverStatus | None:
    """Parse H-Bridge status (0x0D/0x0E).

    INTERNALS.md § Cover/Slide/Awning:
      STATE-ONLY — no control commands published.
      Position: 0xFF = unavailable.
    """
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


def parse_real_time_clock(data: bytes) -> RealTimeClock | None:
    """Parse RealTimeClock (0x20) — 7 bytes after event type."""
    if len(data) < 8:
        return None
    return RealTimeClock(
        year=data[1] + 2000,
        month=data[2],
        day=data[3],
        hour=data[4],
        minute=data[5],
        second=data[6],
        weekday=data[7],
    )


def parse_hour_meter(data: bytes) -> HourMeter | None:
    """Parse HourMeter (0x0F)."""
    if len(data) < 4:
        return None
    # hours are typically a 16-bit value at data[3..4]
    hours_raw = (data[3] << 8 | data[4]) if len(data) >= 5 else data[3]
    return HourMeter(
        table_id=data[1],
        device_id=data[2],
        hours=hours_raw / 10.0,  # 0.1h resolution
    )


def parse_metadata_response(data: bytes) -> list[DeviceMetadata]:
    """Parse GetDevicesMetadata response (event 0x02).

    INTERNALS.md § Device Metadata Retrieval:
      [cmdIdL][cmdIdH][0x02][??][tableId][startId][count] + entries...
      Each entry: [protocol][payloadSize][...payload...]
      Function name at payload offset 0-1 is **BIG-ENDIAN**.
      Accept protocol 1 (Host) AND 2 (IdsCan) with payloadSize == 17.
    """
    if len(data) < 7:
        return []

    table_id = data[4] & 0xFF
    start_id = data[5] & 0xFF
    count = data[6] & 0xFF

    results: list[DeviceMetadata] = []
    offset = 7
    index = 0

    while index < count and offset + 2 < len(data):
        protocol = data[offset] & 0xFF
        payload_size = data[offset + 1] & 0xFF

        if (
            protocol in (METADATA_PROTOCOL_HOST, METADATA_PROTOCOL_IDS_CAN)
            and payload_size == METADATA_PAYLOAD_SIZE_FULL
            and offset + 2 + payload_size <= len(data)
        ):
            # Function name: BIG-ENDIAN 16-bit (INTERNALS.md critical note)
            func_hi = data[offset + 2] & 0xFF
            func_lo = data[offset + 3] & 0xFF
            func_name = (func_hi << 8) | func_lo
            func_instance = data[offset + 4] & 0xFF

            device_id = (start_id + index) & 0xFF
            results.append(
                DeviceMetadata(
                    table_id=table_id,
                    device_id=device_id,
                    function_name=func_name,
                    function_instance=func_instance,
                )
            )

        offset += payload_size + 2
        index += 1

    return results


# ── Dispatcher ────────────────────────────────────────────────────────────


def parse_event(data: bytes) -> Any:
    """Dispatch a decoded COBS frame to the appropriate parser.

    Returns a parsed dataclass, list, or the raw bytes for unknown types.
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
    if event_type == EVENT_DEVICE_LOCK_STATUS:
        return parse_device_lock(data)
    if event_type == EVENT_TANK_SENSOR:
        return parse_tank_status(data)
    if event_type == EVENT_TANK_SENSOR_V2:
        return parse_tank_status_v2(data)
    if event_type == EVENT_DIMMABLE_LIGHT:
        return parse_dimmable_light(data)
    if event_type == EVENT_RGB_LIGHT:
        return parse_rgb_light(data)
    if event_type == EVENT_GENERATOR_GENIE:
        return parse_generator_status(data)
    if event_type == EVENT_HVAC_STATUS:
        return parse_hvac_status(data)
    if event_type in (EVENT_HBRIDGE_1, EVENT_HBRIDGE_2):
        return parse_cover_status(data)
    if event_type == EVENT_HOUR_METER:
        return parse_hour_meter(data)
    if event_type == EVENT_REAL_TIME_CLOCK:
        return parse_real_time_clock(data)
    if event_type == EVENT_DEVICE_COMMAND:
        return parse_metadata_response(data)
    # SESSION_STATUS (0x1A) — heartbeat, just log
    if event_type == EVENT_SESSION_STATUS:
        return None

    # Return raw bytes for truly unknown events
    return data
