# ha-onecontrol

Home Assistant HACS integration for OneControl BLE gateways (Lippert/LCI).

Connects directly to OneControl BLE gateways via the HA Bluetooth stack,
authenticates using the TEA protocol, and creates native HA entities for
RV device monitoring and control.

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Install "OneControl"
3. Restart Home Assistant
4. Go to Settings → Devices & Services → Add Integration → OneControl

### Manual

Copy `custom_components/onecontrol/` to your HA `config/custom_components/` directory.

## Configuration

During setup, the integration discovers OneControl gateways via BLE advertisements.
You will be asked to select your **gateway type** — check the physical hardware:

| Gateway type | How to identify |
|---|---|
| **Push-to-Pair** | Has a physical "Connect" button on the RV control panel |
| **PIN (legacy)** | No Connect button — uses only the 6-digit PIN sticker |

### Push-to-Pair gateways (newer)

1. Select **Push-to-Pair** when prompted
2. Press the physical Connect button on your RV control panel
3. Enter the 6-digit PIN from the gateway sticker
4. Works with both ESPHome Bluetooth Proxy and direct USB adapters

### PIN (legacy) gateways (older)

1. Select **PIN** when prompted
2. Enter the 6-digit PIN from the gateway sticker
3. **Requires a direct USB Bluetooth adapter** — see [PIN Gateway Requirements](#pin-gateway-requirements) below

## PIN Gateway Requirements

Legacy PIN gateways use a BLE passkey exchange during bonding that requires
direct access to the host's BlueZ Bluetooth stack.

### Direct USB Bluetooth adapter (supported)

The integration registers a BlueZ D-Bus agent that provides the passkey
automatically during bonding. No manual steps required beyond entering the PIN
in the config flow. This path is confirmed working.

**Compatible hardware:** the Raspberry Pi's built-in Bluetooth adapter, or any
USB Bluetooth dongle recognized by the HA host.

### ESPHome Bluetooth Proxy (not supported for PIN gateways)

ESPHome proxies forward GATT operations but do not relay BLE passkey/pairing
events back to the HA host. The passkey exchange must happen on the device with
the BLE radio, which is the ESP32 — but the ESP32 has no way to receive the PIN
from HA during a live pairing attempt.

**Push-to-Pair gateways work normally through ESPHome proxies.** Only PIN
gateways are affected.

## Supported Devices

- **Switches** — Relay-controlled devices (lights, water pump, water heaters, tank heater)
- **Dimmable Lights** — Brightness control with Blink/Swell effects (Slow/Medium/Fast)
- **RGB Lights** — Color control with 7 effects (Blink, Swell, Strobe, Color Cycle, etc.)
- **HVAC Climate Zones** — Heat/Cool/Heat+Cool modes, fan speed, temperature setpoints
- **Tank Sensors** — Fresh, grey, black tank levels (%)
- **Cover/Slide Sensors** — H-Bridge status (Opening/Closing/Stopped) — state-only for safety
- **Generator** — Start/stop control with status monitoring
- **System Sensors** — Voltage, temperature, device count, table ID, protocol version
- **In-Motion Lockout** — Safety binary sensor + clear button
- **Data Health** — Binary sensor showing if gateway data stream is active
- **Diagnostics** — One-click state dump from Settings → Devices & Services → OneControl → ⋮ → Download diagnostics
- **DTC Fault Codes** — 1,934 diagnostic trouble codes with HA event firing for gas appliance faults

## License

MIT
