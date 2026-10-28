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
 - Official IDS-CAN `PRODUCT_ID` observed for X180T: `163` (kept in code as `X180T_PRODUCT_ID` for reference).

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

## 10.1 Cover Control and Movement Safety

Covers (awnings, slides, vent covers) are controlled as **momentary H-bridge**
loads. On IDS-CAN BLE gateways the awning/slide device type is
`MomentaryHBridgeType2` (33), and on legacy (non-CAN) gateways H-Bridge action
frames are used. A momentary H-bridge is energized only while the direction
command is actively held — exactly like the physical panel's press-and-hold
button — so HA's discrete open/close/stop taps are translated into a repeating
"held button" in the coordinator:

- **CAN-BLE (`async_can_cover`):** an *active REMOTE_CONTROL session* is opened
  first (the motor controller rejects COMMAND frames without one, reporting
  `RelayMovementStatus.UnableToMoveNoSession`). The direction COMMAND is then
  repeated every `_COVER_COMMAND_REPEAT_S` (0.2 s) by a background repeater task
  until a STOP bumps the per-device generation counter.
- **Non-CAN (`_hbridge_cover_repeater`):** the equivalent H-Bridge ACTION frame
  is resent on the same held-button cadence.

Direction byte mapping (HA logical → IDS-CAN `COMMAND_MODE` byte, decompiled
from the official app's
`LogicalDeviceRelayHBridgeMomentaryCommandType2.ToCommand`):

| HA direction | COMMAND_MODE | Meaning |
|--------------|--------------|---------|
| 0x00 stop    | 0x00         | Stop    |
| 0x01 open    | 0x01         | Forward / extend |
| 0x02 close   | 0x02         | Reverse / retract |

> This is the **command** enum, not the `RELAY_TYPE_2_OUTPUT_STATE` **status**
> enum (`OFF_STOP=0, ON=1, FORWARD_EXTEND=2, REVERSE_RETRACT=3`). An earlier
> build mapped open→0x02 / close→0x03 from the status enum, which made the
> awning retract on "open" — confirmed live via DEVICE_STATUS: sending 0x02 for
> open made the awning report 0xC3 (reverse/closing).

### REMOTE_CONTROL session heartbeat

The X180T motor controller terminates an idle REMOTE_CONTROL session with
`RESPONSE.TIMEOUT` (`0x0F`) after ~1 s, so the session heartbeat must fire well
under that window while a cover is moving. `_RC_SESSION_HEARTBEAT_S = 0.5` sends
a `SESSION_HEARTBEAT` (0x44) every 500 ms. (The official app instead lets the
session lapse and re-opens it with a fresh seed/key — `TendCoreSession` — but
the 0x44 heartbeat path is what has been observed holding the session open here.)

### Travel time + derived safety ceiling

Repeating open/close commands would run the motor indefinitely if a STOP frame
is ever dropped (BLE drop, stuck button, session loss). Two distinct notions
are therefore kept separate:

- **Travel time** — how long one open/close press runs the motor.  The fallback
  is the per-entry option `cover_safety_timeout` (default 6.0 s, clamped 1–60 s),
  with optional per-cover, per-direction overrides under `cover_travel`
  (`{"0c:06": {"extend": 30.0, "retract": 28.0}}`).  Both are read and clamped
  at the point of use in the coordinator, not only in the form schema.
- **Safety ceiling** — the failsafe bound, *derived* from the travel time and
  never entered by hand: `ceiling = travel + clamp(0.2 × travel, 2 s, 5 s)`,
  hard-capped at 90 s.  It is enforced OUTSIDE the repeater loop via
  `asyncio.timeout(ceiling)`, with a `finally` that force-sends STOP, so a
  wedged loop, a generation bump that never lands, or a reconnect-spin still
  stops the motor.  Logging is `CAN BLE: cover safety ceiling … exceeded —
  force-stopping` (WARNING).

### When the travel clock starts

Travel time is measured from the **first COMMAND actually written**, not from
the button press.  Opening the REMOTE_CONTROL session costs anything from under
a second to ~3.5 s on the X180T, and charging that to the travel window made a
5 s setting run ~3.6 s on one press and barely move on the next.  Each repeater
therefore runs in two phases, both bounded from outside their loop:

1. **Start** (`COVER_START_TIMEOUT_S`, 10 s) — wait for the link, ensure the
   session, write the first COMMAND.  If the motor cannot be started inside
   that bound, a bounded STOP is sent and the repeater exits.
2. **Travel** — resend every `_COVER_COMMAND_REPEAT_S` until the travel time
   has elapsed since that first COMMAND.  The derived ceiling is armed here, at
   the first COMMAND, so it bounds the *movement* rather than press-to-stop.

### Failsafe STOP is itself bounded

Every STOP on a failsafe path (start-timeout, travel-complete, ceiling,
`finally`) goes through a bounded send (`COVER_STOP_TIMEOUT_S`, 2 s).  Those
paths run precisely when something already failed — possibly a hung BLE write —
and an unbounded await there would stall the same way, preventing the session
release and task teardown in `finally` from running at all.

A user STOP also bumps the generation counter, so a repeater that outlives its
cancellation exits instead of resending a direction behind the STOP.

Added (2026-09-03) after a runaway awning event; reshaped (2026-10-04) to split
travel time from the derived ceiling per multi-cover review feedback; travel
clock and bounded failsafe (2026-10-25) after multi-cover rig testing.

### Releasing sessions: the panel must get the device back

A REMOTE_CONTROL session belongs to whoever opened it until it is explicitly
closed (request `0x45`), and the device keeps it even when the BLE link drops —
dropping the heartbeat is not enough, and neither is losing the connection.  A
session left open makes the coach's physical panel refuse that device until the
gateway is power-cycled, while Home Assistant keeps working, which makes it look
like a control has simply died.

Every COMMAND needs a session, so every COMMAND path must give it back.  The
cover repeater closes its own session in `finally` (see above).  Everything else
is released by a **janitor**, not on the command path:

- `_rc_session_dirty` records each device with an open, unreleased session and
  the monotonic time of the last COMMAND written in it.  It survives a
  disconnect on purpose — the debt outlives the link.
- `_note_rc_session_command()` refreshes that timestamp from every COMMAND site
  (relay, relay retry, queued replay, cover command).  It runs **before** the
  frame is written, not after: a stamp taken afterwards leaves the previous
  command's grace period in place for the duration of the write, so a janitor
  tick landing inside that write can close the session out from under it.  A
  fresh COMMAND also clears the previous confirmation, which is why the
  confirmation is recorded after the write.
- `_rc_session_janitor()` ticks every `RC_SESSION_JANITOR_INTERVAL_S` (0.5 s) and
  `_async_reap_idle_rc_sessions()` closes a device once it has nothing left to
  hold the session for and no cover movement is using it.  What counts as "left"
  depends on the feedback: a COMMAND the device confirmed from DEVICE_STATUS is
  released after `RC_SESSION_CONFIRMED_RELEASE_S` (0.75 s — the device has
  already applied it), an unconfirmed one after `RC_SESSION_IDLE_RELEASE_S`
  (2 s).  Both are measured from the write, and a fresh COMMAND clears the
  previous confirmation.  "A cover movement holds this session" is decided on
  the device half of the `"tt:dd"` cover key, the same identity
  `_cover_command_tasks` and `covers` use.

  The window is deliberately sized by what the COMMAND needs, not by how long
  confirmation may take.  Holding the session is what locks the coach's physical
  panel out, so a session kept for the length of the relay verification window
  (`_CAN_COMMAND_VERIFY_TIMEOUT_S` / `..._V1_S`, 3 s and 5 s) made the panel
  ignore presses for seconds after every Home Assistant toggle — the first cut
  of this fix did exactly that, and users read it as a 3–5 s lag.  Confirmation
  itself does not need the session: DEVICE_STATUS broadcast frames arrive
  regardless of who holds it.
- A device with a COMMAND still waiting for its feedback is not reaped either.
  `_rc_session_awaiting_confirmation` counts the commands claiming a device
  (`_claim_rc_session()` / `_release_rc_session_claim()`) and the janitor skips
  anything with a claim.  It is taken **before** `_ensure_remote_control_session`
  and dropped when the verification window — first attempt and retry — is over.
  The unconfirmed grace (2 s) is shorter than the V1 relay verify window (5 s),
  so without this the release could race the confirmation the session exists
  for.  It is a count rather than a flag because a second toggle can start
  inside the first one's window; a flag would let the first one finishing
  release the claim the second still needs.  Releasing a device clears any
  claim it still holds.
- The janitor decides and closes under one lock.  On a V1 gateway the seed/key
  handshake holds `_rc_session_lock` for about a second, so a janitor that
  decided to reap before taking the lock could get it after a re-toggle had
  already claimed the device and written its COMMAND — and send `0x45` right
  behind that write.  `_async_reap_idle_rc_sessions()` therefore checks its skip
  conditions before the lock (cheap rejection) **and again while holding it**,
  then calls `_async_close_rc_session_locked()`.
- The janitor also starts with the CAN keepalive, so sessions owed from before a
  reconnect are closed on the next connect, and `async_disconnect()` releases
  everything still owed before the link goes.  That teardown sweep is bounded
  **once** (`RC_SESSION_RELEASE_TIMEOUT_S`) for all of it — a per-device bound
  lets several owed sessions hold up an unload by a multiple of the timeout —
  and anything not released keeps its debt for the next janitor pass.

**Do not close a session on the command path.**  v1.0.48 did exactly that after
relay commands and had to be reverted: the device began terminating sessions
with reason `0x0E`, relay COMMANDs were never confirmed from DEVICE_STATUS, and
switch state reverted a few seconds later.  Closing *after* the command has been
confirmed, from outside the command path, avoids that.

A close that fails keeps its debt and is re-stamped, so the next janitor pass
retries after another grace period instead of spinning on a failing write.
Closing a device that a previous command left behind does not touch the live
session's heartbeat.

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

## 14. APK Parity Analysis (June 2025)

Analysis of the official OneControl Android APK (Xamarin/.NET, decompiled assemblies)
identified the following gaps between the mobile app and this HA integration.

### 14.1 Implemented (this analysis)

- **HVAC Schedule Mode (heat_mode=4):** The APK supports a programmed schedule mode
  (`ProgramedCommand` in ZoneStatus). Added `HVAC_MODE_SCHEDULE = 4` and mapped it to
  HA `HVACMode.AUTO`. Previously unmapped mode 4 fell through to OFF.

- **Electric Heat Capability:** The APK distinguishes gas heat from electric heat
  (`IsElectricHeat` capability). Added `HVAC_CAP_ELECTRIC_HEAT = 0x10` and detection
  for `active_status=4` (electric heat active). Previously only gas (status 5-6) and
  heat pump (status 3) were detected; electric heat was visible as HEATING action but
  not tracked as a capability.

### 14.2 APK Features Present in HA (Verified)

| APK Feature | HA Implementation | Status |
|-------------|-------------------|--------|
| Relay/latching switches | `switch.py` — OneControlSwitch | Matches |
| Generator start/stop | `switch.py` — OneControlGeneratorSwitch | Matches |
| Generator status (state, battery, temp) | `sensor.py` — Generator/Battery/Temp sensors | Matches |
| Generator quiet hours | `binary_sensor.py` — OneControlGeneratorQuietHours | Matches |
| Dimmable lights | `light.py` — OneControlDimmableLight + effects (Blink/Swell) | Matches |
| RGB lights | `light.py` — OneControlRgbLight + effects (Solid/Blink/Transitions/Rainbow) | Matches |
| HVAC zones (mode, fan, setpoints) | `climate.py` — OneControlClimate + presets | Matches |
| HVAC capabilities (Gas, AC, HeatPump, Fan) | `coordinator.py` — observed_hvac_capability | Matches |
| Covers/awnings/slides (open/close/stop) | `cover.py` — OneControlCover | Matches |
| Advanced H-bridge commands (Auto Open/Close, Clear Latch, Home Reset) | `cover.py` — OneControlCoverButton | Matches |
| Tank levels (fresh/grey/black/fuel) | `sensor.py` — OneControlTankSensor | Matches |
| Tank alerts (threshold/connectivity) | `sensor.py` — OneControlTankAlertSensor | Matches |
| Leveler position/status | `sensor.py` — OneControlLevelerPositionSensor | Matches |
| Hour meter | `sensor.py` — OneControlHourMeterSensor | Matches |
| System voltage/temperature | `sensor.py` — Voltage/Temperature/AcVoltage sensors | Matches |
| In-motion lockout detection | `binary_sensor.py` — OneControlInMotionLockout | Matches |
| Clear lockout button | `button.py` — OneControlClearLockoutButton | Matches |
| Refresh metadata | `button.py` — OneControlRefreshMetadataButton | Matches |
| DTC (Diagnostic Trouble Codes) | `protocol/dtc_codes.py` — 1934 codes | Matches |
| Function name mapping | `protocol/function_names.py` — 445 function names | Matches |
| IDS-CAN wire protocol | `protocol/ids_can_wire.py` — frame parsing + composition | Matches |
| COBS/CRC framing | `protocol/cobs.py`, `protocol/crc8.py` | Matches |
| TEA encryption (Step 1/2, CAN-BLE key/seed) | `protocol/tea.py` | Matches |

### 14.3 APK Features NOT in HA

| APK Feature | Reason | Priority |
|-------------|--------|----------|
| **Generator Auto-Start** (`IsAutoRunOnTempAvailable`, timers, schedules) | Requires PID read/write for auto-start configuration. PID addresses need reverse-engineering from APK. | Medium |
| **Leveler Control** (Type 1/3/4/5 button commands, jack movement, calibration, zero point, home jacks, RF remote pairing, cold weather mode, auto-level, auto-ground, panic stop) | Requires IDS-CAN REMOTE_CONTROL session management and leveler-specific command encoding. `IDS_CAN_REQUEST_LEVELER_TYPE_5_CONTROL` (0x60) is defined in `ids_can_wire.py`. | Medium |
| **DC Power / Battery Monitor** (chemistry, capacity, charge state) | Separate device type with its own PIDs. Not exposed via MyRvLink events. | Low |
| **Tire Linc / TPMS** (tire pressure, temperature, sensor battery) | Companion BLE device (`com.idselectronics.linctab.app_remote`), not part of OneControl gateway. | Low |
| **Camera Integration** (firmware, password, settings) | Separate device ecosystem. | Low |
| **Firmware Update / OTA** | `MyRvLinkCommandSoftwareUpdateAuthorization` exists in APK DLLs but is complex and risky without hardware testing. | Low |
| **Cloud/Remote Access** (MyRvCloud, RvCloudIoT) | Deliberately local-only for HA. Cloud DLLs: `OneControl.Remote.MyRvCloud.dll`, `OneControl.Direct.RvCloudIoT.dll`. | Won't implement |
| **TCP/IP Direct Connection** (`OneControl.Direct.MyRvLinkTcpIp.dll`) | BLE-only for HA by design. | Won't implement |
| **Light Groups** (All Lights On/Off) | `AllLightsGroupBehaviorCapability` in APK. Could be implemented as HA scripts. | Low |
| **Awning Wind Sensor / Auto-Protect** | `awning_sensor_wind_alert`, `awning_auto_protected`. May require separate BLE accessory (`IdsCanAccessoryBle`). | Low |
| **Electronic Sway Control** | `electronic_sway_control`, `IsSavingSwayFeature`. Separate chassis system. | Low |
| **Anti-Lock Brakes** | `anti_lock_brakes`, `abs_connect`. Separate chassis system. | Low |
| **Sleep Timer** | `sleep_timer`, `SleepTimerValueText`. Convenience feature, not critical. | Low |
| **Door Lock Power Modes** (normal/low/storage) | Power mode configuration beyond basic lock state. | Low |

### 14.4 Device Categories from APK

The APK organizes devices into the following UI groups (from `OneControl.Devices.dll`):

- **Bed Lifts** — TV lifts, bed lifts, roof lifts
- **Leveling** — Levelers Type 1-6, stabilizers, jacks
- **Lighting** — All light types (dimmable, RGB, relay)
- **Slides** — Room slides (bedroom, kitchen, etc.)
- **Stabilizers** — Front/rear/left/right stabilizers
- **TV Lifts** — TV lift mechanisms
- **Vent Covers** — Bath/kitchen/living room vent covers
- **Router** — Network bridge, WiFi bridge

HA maps these to native entity types (cover, light, switch) based on function name.

### 14.5 HVAC Capability Byte Verification

APK capability flags vs HA implementation:

| APK Flag | HA Constant | Bit | Detected By |
|----------|-------------|-----|-------------|
| `HasAirConditioner` | `HVAC_CAP_AC` | 0x02 | active_status=2 |
| `HasHeatPump` | `HVAC_CAP_HEAT_PUMP` | 0x04 | active_status=3 |
| `IsGasHeat` | `HVAC_CAP_GAS` | 0x01 | active_status=5,6 |
| `IsElectricHeat` | `HVAC_CAP_ELECTRIC_HEAT` | 0x10 | active_status=4 (NEW) |
| `IsMultiSpeedFan` | `HVAC_CAP_MULTISPEED_FAN` | 0x08 | fan_mode=2 (low) |

These match the APK's `OnDeviceCapabilityChanged` event handling in
`LogicalDeviceClimateZone`.
