# OneControl HACS Integration — Technical Specification

## 1. Purpose and Scope

`ha_onecontrol` is a Home Assistant BLE integration for Lippert/LCI OneControl gateways. It provides native entity discovery and control for relays, lights, HVAC zones, covers, tanks, generator telemetry, and diagnostics.

This document describes the current HA-native implementation and excludes mobile bridge specifics.

## 2. Integration Snapshot

- **Domain:** `ha_onecontrol`
- **Primary runtime component:** `OneControlCoordinator`
- **Platforms:** `binary_sensor`, `button`, `climate`, `light`, `sensor`, `switch`
- **Transport:** BLE GATT via Home Assistant Bluetooth stack
  - MyRvLink BLE data transport for original gateways
  - IDS-CAN-over-BLE transport for CAN-BLE gateways such as Unity XZ and experimental Unity X180T
- **Coordinator mode:** push/event-driven (`update_interval=None`)

## 3. Configuration and Entry Setup

- BLE discovery uses Lippert manufacturer advertisement data, known service UUIDs, and the `LCIRemote` local-name prefix.
- Config flow supports two pairing models:
  - push-to-pair gateways
  - PIN-based gateways
- Experimental X180T discovery uses the official app's X180T primary service UUID and modern Lippert TLV advertisement metadata when available.
- Core credentials:
  - `gateway_pin` (required)
  - `bluetooth_pin` (optional override)

## 4. Runtime Lifecycle

1. Entry setup creates coordinator and forwards platforms.
2. Initial connect runs as background task (non-blocking startup).
3. BLE session authenticates and enables notifications.
4. COBS/CRC decode pipeline parses protocol events.
5. Parsed state maps are updated and listener callbacks refresh entities.

## 5. Protocol and Transport Model

### 5.1 Authentication/session flow

The coordinator supports multiple mutually exclusive BLE/authentication families. This reflects the official app architecture, where older MyRvLink gateways, legacy PIN gateways, IDS-CAN BLE gateways, and X180T-style pairable CAN gateways share some GATT services but use different security gates.

For MyRvLink BLE gateways, the coordinator executes a two-step authentication sequence before normal event handling.

- **Auth service:** `00000010-0200-a58e-e411-afe28044e62c`
- **Seed characteristic:** `00000011-0200-a58e-e411-afe28044e62c`
- **Unlock status characteristic:** `00000012-0200-a58e-e411-afe28044e62c`
- **Key characteristic:** `00000013-0200-a58e-e411-afe28044e62c`
- **Auth status characteristic:** `00000014-0200-a58e-e411-afe28044e62c`

Session timing constants include `AUTH_TIMEOUT=10s`, `UNLOCK_VERIFY_DELAY=0.5s`, `NOTIFICATION_ENABLE_DELAY=0.2s`, and `BLE_MTU_SIZE=185`.

IDS-CAN BLE gateways use a different runtime path:

- **CAN service:** `00000000-0200-a58e-e411-afe28044e62c`
- **CAN write characteristic:** `00000001-0200-a58e-e411-afe28044e62c`
- **CAN read/notify characteristic:** `00000002-0200-a58e-e411-afe28044e62c`
- **CAN software part/version characteristic:** `00000004-0200-a58e-e411-afe28044e62c`
- **CAN password unlock characteristic:** `00000005-0200-a58e-e411-afe28044e62c`

CAN-BLE gateways may also expose the key/seed service used by the official app before opening CAN traffic:

- **Key/seed service:** `00000010-0200-a58e-e411-afe28044e62c`
- **Seed/unlock status characteristic:** `00000012-0200-a58e-e411-afe28044e62c`
- **Key characteristic:** `00000013-0200-a58e-e411-afe28044e62c`

The official X180T/CAN-BLE key/seed cipher is `0xC81D7F20`. Unity XZ-style CAN-BLE gateways have been observed working without BLE SMP bonding, relying instead on GATT-level CAN-BLE authentication and IDS-CAN session behavior. X180T appears to be a hybrid: CAN-BLE runtime plus official-app BLE bonding and key/seed unlock.

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

IDS-CAN BLE transport uses CAN wire frames over `CAN_WRITE`/`CAN_READ`, not MyRvLink COBS data frames. V2 BLE CAN notifications are packed with a leading notification type byte:

- `0x01` Packed device advertisement, expanded into synthetic NETWORK, DEVICE_ID, DEVICE_STATUS, and FakeCircuitID IDS-CAN frames
- `0x02` ElevenBit, one reconstructed 11-bit IDS-CAN frame
- `0x03` TwentyNineBit, one reconstructed 29-bit IDS-CAN frame

The coordinator claims a LocalHost IDS-CAN source address, emits NETWORK identity frames, opens REMOTE_CONTROL sessions when needed, and queues CAN commands while the short-lived CAN-BLE connection is between reconnect windows.

### 5.2.1 Lippert manufacturer advertisement parsing

Older gateways can be interpreted using the first byte of Lippert manufacturer data as a legacy `PairingInfo` byte:

- bit 0: push-to-pair button present on bus
- bit 1: pairing currently enabled / Connect button active

Modern IDS-CAN / X180T advertisements use the official app's TLV format instead. Home Assistant/Bleak exposes manufacturer data after the company identifier, so records are parsed as:

`[length][type][payload...]`

`length` includes the type byte, not the length byte.

Known TLV types:

| Type | Name | Payload |
|------|------|---------|
| `0` | `ConnectionInfo` | 2 bytes: status/capability, pairing flags |
| `1` | `BleCanGatewayProtocolVersion` | 1 byte; values `>= 68` map to official gateway version `V2_D` |
| `5` | `PairingInfo` | 1 byte; bit 0 indicates push-to-pair button present |

`ConnectionInfo` status lower nibble maps to official BLE capability:

| Value | Official capability | Integration pairing method |
|-------|---------------------|----------------------------|
| `0` | `DisplayOnly` | PIN/passkey |
| `1` | `DisplayYesNo` | none |
| `2` | `KeyboardOnly` | none |
| `3` | `NoIO` | push-button / Just Works |
| `4` | `KeyboardAndDisplay` | none |

The pairing byte uses bit 0 for pairing supported and bit 1 for pairing available now. Button presence alone is not treated as the full pairing method; official behavior derives pairing method from `ConnectionInfo.BleCapability`.

### 5.2.2 Unity X180T gateway model

Experimental X180T support is based on official app handling rather than local hardware validation.

- **X180T primary discovery service:** `0000000f-0200-a58e-e411-afe28044e62c`
- Official scan result type implements pairable gateway, CAN gateway, manufacturer-data, and key/seed interfaces.
- Runtime connection is `RvGatewayCanConnectionBle`, not MyRvLink.
- Official app stores an empty CAN password for X180T (`string.Empty`) while still using BLE pairing metadata and key/seed exchange.
- X180T uses CAN-BLE runtime service `00000000-0200-a58e-e411-afe28044e62c` after pairing/bonding.
- X180T key/seed cipher is `0xC81D7F20`.

The first experimental release classified X180T and routed it to CAN-BLE, but field logs showed that pre-connect BlueZ Just Works pairing could still fail before service discovery. The follow-up changed X180T push-button handling to more closely match official app ordering: register a Just Works agent, connect GATT first, then call `pair()` post-connect. If bonding or `CAN_READ` subscription fails, the coordinator now fails the attempt instead of marking the gateway authenticated and attempting CAN writes without service discovery.

### 5.3 Identity model

- Canonical device join key: `(table_id, device_id)` encoded as `tt:dd`
- all per-device runtime maps and metadata binding use this key

### 5.4 BLE adapter source pinning

The OneControl gateway requires BLE pairing (LTK/bond) to authenticate the UNLOCK_STATUS characteristic read (GATT ATT layer requires encryption). BlueZ stores LTK bonds **per physical adapter** under `/var/lib/bluetooth/<adapter_mac>/<device_mac>/`. ESPHome BT proxies maintain their own independent bond storage in device NVS flash — inaccessible to BlueZ. Consequently, routing a connection through a proxy that has never bonded to the gateway fails immediately with ATT error status=5 (Insufficient Authentication).

This constraint applies to gateway families that require BLE SMP pairing/bonding. It does not necessarily apply to all CAN-BLE gateways. Unity XZ-style CAN-BLE operation has been observed working through an ESPHome BT proxy, which suggests that this gateway family can operate through ordinary proxied GATT reads/writes/notifications once the application-layer CAN-BLE unlock/session behavior succeeds. In practical terms: gateways requiring BLE SMP pairing need local BlueZ access; CAN-BLE gateways that only require GATT-level authentication may work through ESPHome proxies.

HA's default adapter selection (`async_ble_device_from_address`) picks the "best" source from the scanner pool at connect time, which may resolve to whichever adapter has the strongest RSSI — often a nearby proxy at startup.

**Fix (v1.0.16):** The coordinator learns and persists the adapter source that produced the first successful Step-1 auth.

- On connect, `bluetooth.async_scanner_devices_by_address(hass, address, connectable=True)` is called to enumerate all current scanner candidates.
- If `entry.options[CONF_BONDED_SOURCE]` is set, the coordinator filters candidates to that source and passes the matching `BLEDevice` to `establish_connection`.
- After successful Step-1 auth (`UNLOCK_STATUS` read returns unlocked), `hass.config_entries.async_update_entry` persists `CONF_BONDED_SOURCE = scanner.source` (the hciX MAC or proxy name) to config entry options.
- If the pinned source is not currently in the scanner pool (adapter offline/out of range), the coordinator falls back to `async_ble_device_from_address` and re-learns the new source on the next successful auth.

The `BluetoothScannerDevice` attributes used are `.ble_device` (`BLEDevice`) and `.scanner.source` (str — hciX MAC address for local adapters, proxy hostname for ESPHome proxies).

**Fix (v1.0.19) — Boot connection via local HCI preferred:** The v1.0.16 fix stored the scanner source that produced the first successful auth. On HAOS, the local hci0 adapter is reported by HA's scanner pool with the same MAC that BlueZ advertises (for example, `<adapter_mac>`), not a hostname. However, at cold boot `CONF_BONDED_SOURCE` may still hold a proxy source from a prior session, causing an immediate ATT INSUF_AUTH (error 19) before the fallback re-learns.

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
- PIN-based gateway behavior depends on host BLE capabilities
- gateway authentication is not a single mechanism across all models; BLE SMP bond, MyRvLink TEA unlock, CAN-BLE key/seed, and CAN password unlock may appear independently depending on controller family

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
- **v1.0.30 — IDS-CAN BLE / Unity XZ support:** Added CAN-BLE transport support using `CAN_WRITE`/`CAN_READ`, CAN software part/version decoding, CAN password unlock, key/seed unlock where present, LocalHost source claiming, V1/V2 CAN notification decoding, and REMOTE_CONTROL session handling. Unity XZ-style CAN-BLE has been observed working through ESPHome BT proxy because it appears to rely on GATT-level authentication rather than mandatory BLE SMP bonding.
- **v1.0.31 — Experimental X180T support:** Added the official X180T primary service UUID, modern Lippert TLV manufacturer data parsing, official `ConnectionInfo` pairing-method mapping, X180T gateway-family config data, empty X180T CAN password handling, and X180T routing into CAN-BLE with key/seed cipher `0xC81D7F20`.
- **v1.0.32 — X180T connect-first pairing follow-up:** Updated X180T push-button pairing order to register the Just Works agent, connect GATT first, then call `pair()` post-connect, matching the official app flow more closely. Also added stale-bond cleanup for X180T and prevented CAN-BLE auth from marking the gateway authenticated when service discovery or `CAN_READ` subscription has not completed.
- **v1.0.43 — CAN-BLE NETWORK-frame aggregation:** IDS-CAN NETWORK (`mt=0x00`) frames are broadcast per node, each carrying that node's own protocol version and in-motion-lockout bits. The dispatcher previously overwrote gateway-level state from whichever frame arrived last, so the Protocol Version sensor flapped between every node's value several times a second and churned the recorder. Now tracked per source address and aggregated — in-motion lockout as the MAX across active nodes (parity with the official app's `LogicalDeviceService.InMotionLockoutLevel`), protocol version as the MAX observed on the bus — with a coordinator update pushed only when an aggregate actually changes.

## 12. Known Constraints

- PIN-based gateway paths may be constrained by adapter/proxy stack behavior
- X180T support remains experimental pending hardware validation; it combines BLE pairing/bonding, CAN-BLE runtime, and key/seed unlock in a hybrid model
- gateway firmware/protocol variance can require parser updates
- friendly naming depends on successful metadata completion

## 13. Extension Guidelines

1. Keep `(table_id, device_id)` as the single device identity primitive.
2. Track command ids explicitly for multi-command concurrency safety.
3. Stage multi-part payloads and commit only on validated terminal frames.
4. Add observability counters before introducing new protocol surfaces.
5. Use guarded optimistic windows where delayed echoes are common.
