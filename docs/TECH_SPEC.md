# OneControl HACS Integration — Technical Specification

## 1. Purpose and Scope

`ha_onecontrol` is a Home Assistant integration for Lippert/LCI OneControl gateways. It provides native entity discovery and control for relays, lights, HVAC zones, covers, tanks, generator telemetry, and diagnostics.

This document describes the current HA-native implementation and excludes mobile bridge specifics.

## 2. Integration Snapshot

- **Domain:** `ha_onecontrol`
- **Primary runtime component:** `OneControlCoordinator`
- **Platforms:** `binary_sensor`, `button`, `climate`, `light`, `sensor`, `switch`
- **Transport:** BLE GATT via Home Assistant Bluetooth stack; experimental IDS CAN-to-Ethernet bridge path
- **Coordinator mode:** push/event-driven (`update_interval=None`)

## 3. Configuration and Entry Setup

- BLE discovery uses Lippert manufacturer advertisement data.
- Ethernet setup can optionally prefill host/port using UDP bridge advertisements (`Mfg=IDS` or `Product=CAN_TO_ETHERNET_GATEWAY`).
- Config flow supports two transport families:
  - BLE transport
  - Ethernet bridge transport (experimental)
- BLE pairing models include:
  - push-to-pair gateways
  - legacy PIN gateways
- Core credentials:
  - `gateway_pin` (required)
  - `bluetooth_pin` (optional override)
  - `eth_host`/`eth_port` (Ethernet mode)

## 4. Runtime Lifecycle

1. Entry setup creates coordinator and forwards platforms.
2. Initial connect runs as background task (non-blocking startup).
3. Transport session is established (BLE authenticate+notify, or Ethernet socket connect).
4. COBS/CRC decode pipeline parses protocol events.
5. Parsed state maps are updated and listener callbacks refresh entities.

## 5. Protocol and Transport Model

### 5.1 Authentication/session flow

The coordinator executes a two-step authentication sequence before normal event handling.

- **Auth service:** `00000010-0200-a58e-e411-afe28044e62c`
- **Seed characteristic:** `00000011-0200-a58e-e411-afe28044e62c`
- **Unlock status characteristic:** `00000012-0200-a58e-e411-afe28044e62c`
- **Key characteristic:** `00000013-0200-a58e-e411-afe28044e62c`
- **Auth status characteristic:** `00000014-0200-a58e-e411-afe28044e62c`

Session timing constants include `AUTH_TIMEOUT=10s`, `UNLOCK_VERIFY_DELAY=0.5s`, `NOTIFICATION_ENABLE_DELAY=0.2s`, and `BLE_MTU_SIZE=185`.

### 5.2 Frame handling

- COBS framing with CRC checks
- typed event parsing for relay/light/HVAC/tank/system events
- unknown or invalid frame discard with counters

Data transport uses:

- **Data service:** `00000030-0200-a58e-e411-afe28044e62c`
- **Write characteristic:** `00000033-0200-a58e-e411-afe28044e62c`
- **Read/notify characteristic:** `00000034-0200-a58e-e411-afe28044e62c`

MyRvLink event byte identifiers include (non-exhaustive):

- `0x01` gateway info, `0x02` command response, `0x05/0x06` relay, `0x08` dimmable, `0x09` RGB,
- `0x0B` HVAC, `0x0C/0x1B` tank, `0x0D/0x0E` h-bridge, `0x0F` hour meter, `0x20` RTC.

### 5.3 Identity model

- Canonical device join key: `(table_id, device_id)` encoded as `tt:dd`
- all per-device runtime maps and metadata binding use this key

### 5.4 BLE adapter source pinning

The OneControl gateway requires BLE pairing (LTK/bond) to authenticate the UNLOCK_STATUS characteristic read (GATT ATT layer requires encryption). BlueZ stores LTK bonds **per physical adapter** under `/var/lib/bluetooth/<adapter_mac>/<device_mac>/`. ESPHome BT proxies maintain their own independent bond storage in device NVS flash — inaccessible to BlueZ. Consequently, routing a connection through a proxy that has never bonded to the gateway fails immediately with ATT error status=5 (Insufficient Authentication).

HA's default adapter selection (`async_ble_device_from_address`) picks the "best" source from the scanner pool at connect time, which may resolve to whichever adapter has the strongest RSSI — often a nearby proxy at startup.

**Fix (v1.0.16):** The coordinator learns and persists the adapter source that produced the first successful Step-1 auth.

- On connect, `bluetooth.async_scanner_devices_by_address(hass, address, connectable=True)` is called to enumerate all current scanner candidates.
- If `entry.options[CONF_BONDED_SOURCE]` is set, the coordinator filters candidates to that source and passes the matching `BLEDevice` to `establish_connection`.
- After successful Step-1 auth (`UNLOCK_STATUS` read returns unlocked), `hass.config_entries.async_update_entry` persists `CONF_BONDED_SOURCE = scanner.source` (the hciX MAC or proxy name) to config entry options.
- If the pinned source is not currently in the scanner pool (adapter offline/out of range), the coordinator falls back to `async_ble_device_from_address` and re-learns the new source on the next successful auth.

The `BluetoothScannerDevice` attributes used are `.ble_device` (`BLEDevice`) and `.scanner.source` (str — hciX MAC address for local adapters, proxy hostname for ESPHome proxies).

**Fix (v1.0.19) — Boot connection via local HCI preferred:** The v1.0.16 fix stored the scanner source that produced the first successful auth. On HAOS, the local hci0 adapter is reported by HA's scanner pool with the same MAC that BlueZ advertises (e.g. `E0:D3:62:EA:52:52`), not a hostname. However, at cold boot `CONF_BONDED_SOURCE` may still hold a proxy source from a prior session, causing an immediate ATT INSUF_AUTH (error 19) before the fallback re-learns.

The v1.0.19 fix adds:
- `async_get_local_adapter_macs()`: queries the BlueZ D-Bus `org.bluez.Adapter1` objects to enumerate all local HCI adapter MAC addresses.
- `async_is_locally_bonded(address)`: checks `org.bluez.Device1.Paired` to confirm a BlueZ bond exists for the gateway.

On connect, if a local HCI adapter is available in the scanner pool *and* BlueZ confirms a bond, that adapter is strongly preferred over any ESPHome proxy for that candidate set, regardless of RSSI. A single first-attempt error 19 at cold boot (BlueZ SMP not yet ready) is accepted as unavoidable gateway timing behavior.

### 5.5 RGB light protocol

RGB light status is delivered in `0x09` frames with the following layout (confirmed from Android `DeviceStatusParser.kt` / `RgbLightStatus`):

```
[EventType (1)][DeviceTableId (1)][DeviceId (1)][StatusBytes (8)]...
```

Each device occupies 9 bytes: `DeviceId (1) + StatusBytes (8)`. The 8 `StatusBytes` map as:

| Offset | Field        | Notes |
|--------|--------------|-------|
| 0      | Mode         | 0=Off, 1=Solid, 2=Blink, 4=Jump3, 5=Jump7, 6=Fade3, 7=Fade7, 8=Rainbow, 127=Restore |
| 1      | Red          | 0–255 |
| 2      | Green        | 0–255 |
| 3      | Blue         | 0–255 |
| 4      | AutoOff      | minutes (0xFF = disabled) |
| 5      | IntervalHi   | effect interval, big-endian high byte |
| 6      | IntervalLo   | effect interval, big-endian low byte |
| 7      | Reserved     | |

`isOn = mode > 0`. There is no brightness byte; HA-facing brightness is derived as `max(R, G, B)`.

ActionRgb (command `0x44`) SOLID wire format:
```
[CmdId_lo][CmdId_hi][0x44][TableId][DeviceId][mode][R][G][B][autoOff]
```
`autoOff=0xFF` (disabled) is the correct default; `autoOff=0` means auto-off after 0 minutes and causes the device to extinguish the light immediately after the command is received.

Brightness control in HA is implemented by scaling R/G/B channel values proportionally: `ch_scaled = min(255, round(ch * brightness / max(R, G, B)))`.

## 6. State and Entity Model

- Switch entities map relay actions and status.
- Light entities include dimmable and RGB variants.
- Climate entities model per-zone HVAC mode/fan/setpoint state.
- Sensor/binary/button entities expose tanks, system status, lockout, diagnostics, and controls.

## 7. Command and Control Surface

- Relay and light action commands route through coordinator write path.
- HVAC command path includes pending-window suppression and setpoint retry logic.
- Metadata refresh and lockout/maintenance actions are exposed via platform controls.

Command builder wire format is:

`[cmd_id_lsb][cmd_id_msb][command_type][payload...]`

Key command types:

- `0x01` GetDevices
- `0x02` GetDevicesMetadata
- `0x40` switch action
- `0x41` h-bridge action
- `0x42` generator action
- `0x43` dimmable action
- `0x44` RGB action
- `0x45` HVAC action

Metadata response handling is explicitly correlated by cmd-id with response type semantics:

- `0x01` success-multi (staging)
- `0x81` success-complete (commit)
- `0x82` failure (including `0x0F` rejection path with retry scheduling)

Staged metadata commit requires count/CRC consistency and guards against cross-command contamination via pending cmd-id maps.

## 8. Reliability and Recovery

- background startup connection pattern
- reconnect backoff with cap
- heartbeat/liveness maintenance
- stale-stream timeout reconnection
- command-correlation counters for drift detection

Core operational timers:

- `HEARTBEAT_INTERVAL=5s`
- `RECONNECT_BACKOFF_BASE=5s`, `RECONNECT_BACKOFF_CAP=120s`
- `STALE_CONNECTION_TIMEOUT=300s`
- HVAC guard/retry constants: `8s` (mode/fan), `20s` (setpoint), `70s` (preset), setpoint retry delay `5s`, max retries `3`
- Switch optimistic guard: `1s`

## 9. Diagnostics and Observability

- integration diagnostics export coordinator state and protocol counters
- metadata correlation and unknown-command statistics retained for troubleshooting
- entity availability reflects connection and data freshness

## 10. Security and Safety Notes

- cover behavior is conservative and state-oriented for movement safety
- sensitive key-schedule values are not stored as plain constants
- legacy PIN behavior depends on host BLE capabilities

## 11. Evolution Notes (Commit History)

Recent trajectory includes:

- **v1.0.16 — BLE adapter source pinning:** Eliminated post-reboot connection failures caused by HA routing the gateway connection through an ESPHome proxy that lacks the BlueZ bond. The coordinator now auto-learns and persists the bonded adapter source (`CONF_BONDED_SOURCE`) on first successful auth and pins all subsequent connections to it. See §5.4 for full design rationale.
- metadata orchestration hardening (gating, staged commit, retry behavior)
- startup/connect resiliency improvements
- HVAC command parity improvements
- relay bounce suppression
- migration to `ha_onecontrol` domain naming
- **v1.0.19 — Local HCI adapter preference at boot:** Added `async_get_local_adapter_macs()` (BlueZ D-Bus `org.bluez.Adapter1` enumeration) and `async_is_locally_bonded()` (BlueZ `Device1.Paired` check). At connect time the coordinator now strongly prefers a bonded local HCI adapter over any ESPHome proxy, eliminating the ATT INSUF_AUTH error that occurred at cold boot when `CONF_BONDED_SOURCE` held a stale proxy source. See §5.4 for design detail.
- **v1.0.20 — RGB light fixes (issue #1):** Three bugs corrected: (1) `auto_off` command field defaulted to `0` (immediate auto-off) instead of `0xFF` (disabled) — lights turned on but the device extinguished them immediately; (2) the `0x09` status frame `AutoOff` byte (offset 4 of StatusBytes) was misread as brightness, causing HA to always report 0% brightness; (3) the `ATTR_BRIGHTNESS` kwarg was ignored on turn-on. Brightness is now derived as `max(R,G,B)` from the status frame and encoded by proportional R/G/B channel scaling on commands. See §5.5 for RGB protocol detail.

## 12. Known Constraints

- legacy PIN paths may be constrained by adapter/proxy stack behavior
- gateway firmware/protocol variance can require parser updates
- friendly naming depends on successful metadata completion

## 13. Extension Guidelines

1. Keep `(table_id, device_id)` as the single device identity primitive.
2. Track command ids explicitly for multi-command concurrency safety.
3. Stage multi-part payloads and commit only on validated terminal frames.
4. Add observability counters before introducing new protocol surfaces.
5. Use guarded optimistic windows where delayed echoes are common.
