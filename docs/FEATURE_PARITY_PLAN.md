# OneControl HACS Integration — Feature Parity Plan

> Goal: Full feature parity with the Android BLE Plugin Bridge OneControl implementation.  
> Baseline: PoC scaffold (commit 318710d + pairing fixes) — BLE connect, pair, TEA auth, COBS decode, RvStatus + GatewayInfo parsing working end-to-end through ESPHome BT proxy.

---

## Current State (what works today)

| Layer | Status |
|---|---|
| BLE discovery (ESPHome proxy) | ✅ Working |
| PushButton pairing ("Just Works") | ✅ Working |
| TEA Step 1 auth (UNLOCK_STATUS) | ✅ Working |
| TEA Step 2 auth (SEED) | ✅ Working |
| COBS stream decode + CRC8 | ✅ Working |
| GatewayInformation (0x01) | ✅ Parsed (table_id, device_count) |
| RvStatus (0x07) | ✅ Parsed → voltage/temp sensors |
| Config flow (BLE discovery + manual) | ✅ Working |
| Retry with backoff (3 attempts) | ✅ Working |

**Files: 10 Python modules, ~2,100 lines**

---

## Phase 1 — Device Discovery & Friendly Naming

**Priority: HIGH — foundation for all entity naming**

### 1.1 GetDevices / GetDevicesMetadata Commands
- [ ] Implement `MyRvLinkCommandBuilder` — COBS-encoded command frames for:
  - `GetDevices` (0x01): `[cmdId_lo][cmdId_hi][0x01][tableId][startId=0x00][maxCount=0xFF]`
  - `GetDevicesMetadata` (0x02): `[cmdId_lo][cmdId_hi][0x02][tableId][startId=0x00][maxCount=0xFF]`
- [ ] Add auto-incrementing command ID counter in coordinator
- [ ] Send `GetDevicesMetadata` automatically after GatewayInformation (0x01) received
- [ ] Parse `handleCommandResponse` (0x02) event:
  - Route cmdType 0x01 → `handleGetDevicesResponse`
  - Route cmdType 0x02 → `handleGetDevicesMetadataResponse`
  - Handle response types: 0x01 (success, more coming), 0x81 (success, complete), 0x82 (error)
- [ ] Parse metadata entries: `[protocol][payloadSize][fnNameHi][fnNameLo][fnInstance][capability][...17 bytes]`

### 1.2 FunctionNameMapper (445 entries)
- [ ] Port `FunctionNameMapper.kt` → `protocol/function_names.py`
  - 445-entry `Dict[int, str]` mapping function IDs to friendly names
  - `get_friendly_name(function_name: int, function_instance: int) -> str`
  - Instance suffix: "Slide 1", "Slide 2", etc.
- [ ] Fallback naming for devices without metadata (by entity type)

### 1.3 DeviceMetadata Storage
- [ ] `DeviceMetadata` dataclass: `table_id`, `device_id`, `function_name`, `function_instance`, `friendly_name`, `raw_capability`
- [ ] Store metadata in coordinator: `Dict[int, DeviceMetadata]` keyed by `(table_id << 8) | device_id`
- [ ] Persist metadata in config entry options or HA storage (survives restarts)
- [ ] Use `friendly_name` for HA entity `name` and `unique_id` construction

### 1.4 Device Registry Integration
- [ ] Register each CAN device as an HA `DeviceInfo` under the gateway device
  - `identifiers`: `{(DOMAIN, f"{mac}_{table_id}_{device_id}")}`
  - `name`: friendly name from FunctionNameMapper
  - `via_device`: gateway device
  - `model`: derive from event type (e.g., "Relay", "Dimmable Light", "HVAC Zone")

---

## Phase 2 — Full Event Parsing (all device types)

**Priority: HIGH — read-only state for all devices**

### 2.1 Already Implemented
- [x] GatewayInformation (0x01) — parsed
- [x] RvStatus (0x07) — voltage + temperature sensors
- [x] RelayStatus (0x05, 0x06) — parsed (dataclass exists, no platform)
- [x] DeviceOnlineStatus (0x03) — parsed (dataclass exists, no platform)
- [x] DimmableLightStatus (0x08) — parsed (dataclass exists, no platform)
- [x] TankSensorStatus (0x0C) — parsed (dataclass exists, no platform)
- [x] TankSensorStatusV2 (0x1B) — parsed (dataclass exists, no platform)
- [x] HvacStatus (0x0B) — parsed (dataclass exists, no platform)
- [x] HBridgeStatus (0x0D, 0x0E) — parsed (dataclass exists, no platform)

### 2.2 Missing Event Parsers
- [ ] **DeviceLockStatus (0x04)** — extended format (≥8 bytes: system lockout level + bitfield) and legacy format (<8 bytes: per-device lock)
- [ ] **RgbLightStatus (0x09)** — multi-device per frame, 9 bytes per device: `[deviceId][mode][R][G][B][autoOff][intervalHi][intervalLo][reserved]`, 8 modes
- [ ] **GeneratorGenieStatus (0x0A)** — multi-device, 6 bytes per: `[deviceId][statusByte][battMSB][battLSB][tempMSB][tempLSB]`, 5 states, battery/temp 8.8 fixed-point
- [ ] **HourMeterStatus (0x0F)** — multi-device, 6 bytes per: `[deviceId][opSec3..0][statusBits]`, runtime in seconds, 6 status flags
- [ ] **RealTimeClock (0x20)** — diagnostic logging
- [ ] **DeviceSessionStatus (0x1A)** — heartbeat acknowledgement, diagnostic logging

### 2.3 Enhance Existing Parsers
- [ ] `GatewayInformation` — add `device_table_crc` (bytes 5-8), `metadata_table_crc` (bytes 9-12), use for change detection
- [ ] `RelayStatus` — add DTC parsing from extended 9-byte frames (bytes 5-6 = DTC code)
- [ ] `HvacStatus` — add capability detection from observed status bits (learns gas/AC/heat-pump/multi-speed over time)

---

## Phase 3 — HA Entity Platforms

**Priority: HIGH — make parsed data visible as entities**

### 3.1 Switch Platform (`switch.py`)
- [ ] `OneControlSwitch` — for RelayStatus (0x05, 0x06) devices
  - State from relay status event (ON/OFF)
  - `async_turn_on()` / `async_turn_off()` → ActionSwitch command
  - Dynamic creation from DeviceOnline + metadata
  - Friendly name from FunctionNameMapper

### 3.2 Light Platform (`light.py`)
- [ ] `OneControlDimmableLight` — for DimmableLightStatus (0x08)
  - Brightness 0–255, effects: Solid/Blink/Swell
  - `async_turn_on(brightness=)` → ActionDimmable command
  - `async_turn_off()` → ActionDimmable OFF
  - Pending command guard (12s suppress mismatches)
- [ ] `OneControlRgbLight` — for RgbLightStatus (0x09)
  - RGB color, 8 effects (Solid/Blink/Jump3/Jump7/Fade3/Fade7/Rainbow/Restore)
  - JSON command format matching HA light schema

### 3.3 Climate Platform (`climate.py`)
- [ ] `OneControlClimate` — for HvacStatus (0x0B)
  - Modes: OFF, HEAT, COOL, HEAT_COOL
  - Fan modes: AUTO, HIGH, LOW
  - Preset modes: "Prefer Gas", "Prefer Heat Pump"
  - Actions: off/idle/cooling/heating
  - Current temp (indoor), target temp low/high
  - Outdoor temp as extra state attribute
  - Setpoint write with verification/retry (re-send if gateway doesn't confirm within 5s)
  - Capability detection: learn available modes from observed status events

### 3.4 Cover Platform (sensor only — SAFETY)
- [ ] `OneControlCoverSensor` — for HBridgeStatus (0x0D, 0x0E)
  - Read-only sensor: "opening" / "closing" / "stopped" / "unknown"
  - Position 0–100 if reported
  - **NO cover commands** — explicit safety decision (no limit switches, no overcurrent protection on RV awnings/slides)

### 3.5 Sensor Platform (expand `sensor.py`)
- [ ] **Tank sensors** — TankLevel (0x0C, 0x1B): level 0–100%, auto-created per device
- [ ] **Generator battery voltage** — from GeneratorGenie (0x0A)
- [ ] **Generator temperature** — from GeneratorGenie (0x0A), only if supported
- [ ] **Generator state** — "off"/"priming"/"starting"/"running"/"stopping"
- [ ] **Hour meter** — runtime hours, device_class=duration
- [ ] **HVAC outdoor temp** — separate sensor for outdoor temperature

### 3.6 Binary Sensor Platform (`binary_sensor.py`)
- [ ] **Generator Quiet Hours** — from GeneratorGenie (0x0A)
- [ ] **Device Online** — from DeviceOnline (0x03)
- [ ] **System Lockout Active** — from DeviceLockStatus (0x04)
- [ ] **Diagnostic: Connected** — coordinator connected state
- [ ] **Diagnostic: Authenticated** — coordinator auth state
- [ ] **Diagnostic: Data Healthy** — connected + authenticated + recent data

### 3.7 Button Platform (`button.py`)
- [ ] **Refresh Metadata** — sends GetDevicesMetadata command
- [ ] **Clear Lockout** — two-step CAN sequence (0x55 arm → 100ms → 0xAA clear)

---

## Phase 4 — Command Sending (device control)

**Priority: HIGH — control devices, not just read state**

### 4.1 Command Encoder (`protocol/commands.py`)
- [ ] `CommandBuilder` class with auto-incrementing command IDs
- [ ] Frame format: `[cmdId_lo][cmdId_hi][cmdType][...payload]` → COBS encode with CRC → write to DATA_WRITE (0x0033)
- [ ] Command types:
  - `build_action_switch(table_id, device_id, state: bool)` → 0x40
  - `build_action_dimmable(table_id, device_id, mode, brightness, duration)` → 0x43
  - `build_action_dimmable_effect(table_id, device_id, effect, params)` → 0x43 (12-byte)
  - `build_action_rgb(table_id, device_id, mode, r, g, b, ...)` → 0x44
  - `build_action_hvac(table_id, device_id, heat_mode, heat_source, fan_mode, low_temp, high_temp)` → 0x45
  - `build_action_generator(table_id, device_id, state: bool)` → 0x42
  - `build_get_devices(table_id)` → 0x01
  - `build_get_devices_metadata(table_id)` → 0x02
  - `build_clear_lockout()` → special 0x55/0xAA sequence
- [ ] Write method on coordinator: `async_send_command(frame: bytes)`

### 4.2 Optimistic Updates & Debouncing
- [ ] Optimistic state update on command send (immediate UI feedback)
- [ ] Pending command guard for dimmable lights (12s window to suppress bouncing)
- [ ] HVAC setpoint verification with retry (re-send if not confirmed within 5s)
- [ ] Command acknowledgement tracking via CommandResponse (0x02)

---

## Phase 5 — Connection Health & Diagnostics

**Priority: MEDIUM — reliability and observability**

### 5.1 Heartbeat
- [ ] Send `GetDevices` command every 5 seconds (keeps connection alive, triggers device status broadcasts)
- [ ] Track `last_successful_operation_time`
- [ ] Data freshness threshold: 15s without events → data_healthy = False

### 5.2 Reconnection Logic
- [ ] Automatic reconnect on disconnect with exponential backoff
- [ ] Peer-disconnect tracking (error 19): after 3 consecutive → backoff 5s → 10s → 20s → 40s (cap 120s)
- [ ] Reset consecutive count on successful auth
- [ ] Stale connection detection: connected + authenticated but no events for 5 minutes → force reconnect
- [ ] Clean disconnect: stop notifications, disconnect BLE, reset all state

### 5.3 DTC Codes
- [ ] Port `DtcCodes.kt` → `protocol/dtc_codes.py` (1,934 entries)
  - `get_dtc_name(code: int) -> str`
  - `is_fault(code: int) -> bool`
- [ ] Surface DTC info as extra state attributes on relay entities (gas appliances)
- [ ] Fire HA events for fault codes (`onecontrol_dtc_fault`)

### 5.4 Diagnostic Entities
- [ ] Binary sensors: `connected`, `authenticated`, `data_healthy`, `lockout_active`
- [ ] Sensor: `signal_strength` (if RSSI available through ESPHome proxy)
- [ ] Sensor: `gateway_device_count`
- [ ] All diagnostic entities use `entity_category=DIAGNOSTIC`

---

## Phase 6 — PIN-Based Pairing (Legacy Gateways)

**Priority: MEDIUM — needed for non-PushButton gateways**

### 6.1 BlueZ D-Bus Pairing Agent
- [ ] Implement `org.bluez.Agent1` interface via `dbus_fast`:
  - `RequestPasskey(device) → uint32` — return configured PIN
  - `RequestConfirmation(device, passkey)` — accept
  - `Release()` — cleanup
  - `Cancel()` — handle cancellation
- [ ] Register agent at `/org/bluez/agent/onecontrol` before `client.connect()`
- [ ] Unregister agent after pairing completes
- [ ] Config flow: show BT PIN field when advertisement indicates PIN pairing method

### 6.2 ESPHome Proxy Compatibility
- [ ] Investigate whether ESPHome BT proxy forwards passkey pairing
- [ ] Possible limitation: proxy may only support "Just Works" — if so, document that PIN gateways require a local BT adapter
- [ ] Fallback: if pairing fails with agent, try without (in case already bonded)

---

## Phase 7 — Safety Features

**Priority: HIGH — RV-specific safety is non-negotiable**

### 7.1 Cover Control Disabled
- [ ] `HBridgeStatus` creates sensor-only entities (no `CoverEntity`)
- [ ] Document safety rationale: RV awnings and slides lack limit switches and overcurrent protection — remote operation risks mechanical damage or injury
- [ ] Log warning if cover command is somehow attempted

### 7.2 In-Motion Lockout
- [ ] Parse system lockout level from DeviceLockStatus (0x04)
- [ ] When lockout active: disable command sending on affected entities
- [ ] Surface as binary sensor + attribute on all controlled entities
- [ ] **Clear Lockout** button: two-step 0x55 → 0xAA sequence (only when RV is stationary)

### 7.3 DTC Fault Alerting
- [ ] Fire `onecontrol_dtc_fault` event when DTC ≠ 0 on gas appliances
- [ ] Include `dtc_code`, `dtc_name`, `device_name`, `is_fault` in event data
- [ ] Users can build HA automations for critical faults (generator failed, battery voltage, overcurrent)

### 7.4 HVAC Guards
- [ ] HVAC command sending requires at least one valid status event received first (capability detection)
- [ ] Prevent sending HVAC commands with stale or unknown zone state

---

## Phase 8 — Polish & Distribution

**Priority: LOW — after core functionality is solid**

### 8.1 HACS Distribution
- [ ] Create GitHub repository (`custom-components/ha-onecontrol` or personal)
- [ ] Add `hacs.json` with correct schema
- [ ] Add to HACS default repository list (or document custom repo install)
- [ ] Versioned releases with changelog

### 8.2 Logging & Performance
- [ ] Reduce INFO logging to DEBUG for stream events (currently flooding RvStatus + GatewayInfo every second)
- [ ] Add configurable log level option
- [ ] Profile COBS decode + event parse performance under load

### 8.3 Tests
- [ ] Reinstall pytest (currently missing from environment)
- [ ] Add test vectors for all event parsers with real captured data
- [ ] Add command builder tests (encode → decode roundtrip)
- [ ] Add FunctionNameMapper tests
- [ ] Integration tests with mock BLE client

### 8.4 Documentation
- [ ] README with setup instructions, ESPHome proxy requirements, supported devices
- [ ] Document PIN vs PushButton pairing differences
- [ ] Document safety decisions (cover control, lockout)
- [ ] Add device compatibility matrix

---

## Implementation Order (recommended)

| Sprint | Phases | Effort | Outcome |
|---|---|---|---|
| **Sprint 1** | 1 + 2.2 + 4.1 | 3–4 days | Device discovery, all parsers, command encoding |
| **Sprint 2** | 3 (all platforms) | 3–4 days | Full entity visibility — switches, lights, climate, sensors |
| **Sprint 3** | 4.2 + 5 + 7 | 2–3 days | Command UX polish, heartbeat, reconnect, safety |
| **Sprint 4** | 6 | 1–2 days | PIN pairing (needs hardware test) |
| **Sprint 5** | 8 | 1–2 days | HACS packaging, tests, docs |

**Total estimated: 10–15 days of focused development**

---

## File Plan (new + modified)

### New Files
| File | Purpose |
|---|---|
| `protocol/function_names.py` | 445-entry name mapping |
| `protocol/commands.py` | Command builder + COBS encode for outbound frames |
| `protocol/dtc_codes.py` | 1,934 DTC code lookup |
| `switch.py` | Relay switch platform |
| `light.py` | Dimmable + RGB light platform |
| `climate.py` | HVAC climate platform |
| `binary_sensor.py` | Online, diagnostic, lockout sensors |
| `button.py` | Refresh metadata, clear lockout |

### Modified Files
| File | Changes |
|---|---|
| `const.py` | Add event types 0x04, 0x09, 0x0A, 0x0F, 0x1A, 0x20; command constants |
| `events.py` | Add RgbLight, GeneratorGenie, HourMeter, DeviceLock, RealTimeClock parsers; enhance GatewayInfo, Relay, Hvac |
| `coordinator.py` | Add command sending, heartbeat, watchdog, reconnect backoff, device metadata management, event routing to platforms |
| `sensor.py` | Add tank, generator, hour meter, HVAC outdoor temp, cover state sensors |
| `__init__.py` | Forward setup to all new platforms |
| `manifest.json` | Bump version |
| `config_flow.py` | Minor — PIN flow improvements |
| `strings.json` / `translations/en.json` | Add strings for new entities |

### Estimated Line Count
- Current: ~2,100 lines
- Target: ~8,000–10,000 lines (including 445-name map + 1,934 DTC codes)
