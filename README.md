# ha-onecontrol

Home Assistant HACS integration for OneControl BLE gateways (Lippert/LCI).

Connects directly to OneControl BLE gateways via the HA Bluetooth stack
(including ESPHome BT proxy support), authenticates using the TEA protocol,
and creates native HA entities for RV device monitoring and control.

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Install "OneControl"
3. Restart Home Assistant
4. Go to Settings → Devices & Services → Add Integration → OneControl

### Manual

Copy `custom_components/onecontrol/` to your HA `config/custom_components/` directory.

## Configuration

During setup, the integration will discover OneControl gateways via BLE advertisements.
You'll need the **6-digit PIN** from the sticker on your gateway.

**Push-to-Pair gateways (newer, e.g. Unity X270D):**
- Press the physical "Connect" button on your RV control panel
- Enter the PIN when prompted
- Works with ESPHome Bluetooth Proxy and direct USB adapters

**Legacy PIN gateways (older):**
- Enter the PIN when prompted (used for both BLE bonding and protocol auth)
- See [PIN Gateway Notes](#pin-gateway-notes) below

## PIN Gateway Notes

Legacy (PIN-based) gateways require a passkey exchange during BLE bonding.
The integration supports two pairing paths:

1. **Direct USB Bluetooth adapter** — Preferred. The integration registers a
   D-Bus BlueZ agent that provides the passkey automatically during bonding.
2. **ESPHome Bluetooth Proxy** — Fallback. The integration attempts `pair()`
   through the proxy's Bluetooth stack. **This is currently untested** — it
   depends on whether the ESPHome proxy forwards passkey requests from the
   ESP32's NimBLE stack. If you have a PIN gateway behind an ESPHome proxy,
   please report your results.

If PIN pairing fails, the integration will still attempt to connect — it may
succeed if the device was previously bonded from another session.

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
