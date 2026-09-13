# ha-onecontrol

Home Assistant HACS integration for OneControl BLE gateways (Lippert/LCI).

Connects directly to OneControl BLE gateways via the HA Bluetooth stack, authenticates using the TEA protocol, and creates native HA entities for RV device monitoring and control.
> **Disclaimer:** This is an independent community integration and is not affiliated with, endorsed by, or supported by Lippert Components or any of its affiliates. Use it at your own risk.
## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS

2. Install "OneControl"

3. Restart Home Assistant

4. Go to Settings → Devices & Services → Add Integration → OneControl

### Manual

Copy `custom\_components/onecontrol/` to your HA `config/custom\_components/` directory.

## Configuration

During setup, the integration discovers OneControl gateways via BLE advertisements. You will be asked to select your **gateway type** — check your RV's control panel:

| Gateway type | How to identify |
| - | - |
| **Push-to-Pair** | Has a "Connect" control on the RV panel — a physical button on some models, an on-screen button on touchscreen panels such as the Unity X270L |
| **PIN-based** | No Connect control — uses only the 6-digit PIN sticker |

> **The Connect-button test isn't always definitive.** Some gateways — notably the **Unity X1.5** — ship in both variants: some units require PIN bonding, while others use a "just works" pairing that the integration handles as **Push-to-Pair**, even though the unit has only a PIN sticker and no Connect button. If you pick **PIN** and setup fails to connect (or repeatedly disconnects), **delete the entry and re-add it as Push-to-Pair** — you can leave the button step and just enter the PIN. On these gateways the PIN is still used, just at a different layer, so Push-to-Pair with your sticker PIN is the correct choice even with no physical button to press.

### Push-to-Pair gateways (newer)

1. Select **Push-to-Pair** when prompted

2. Press the Connect control on your RV panel **while the config flow is waiting on that
   step** — physical button on some models, on-screen button on touchscreen panels

3. Enter the 6-digit PIN from the gateway sticker

4. Works with both ESPHome Bluetooth Proxy and direct USB adapters

> **The order matters.** If you enter the PIN without having pressed Connect while the flow
> was waiting, setup appears to succeed — every entity is created — but they all sit
> **unavailable**, because the gateway never authenticated the session. That symptom (a full
> set of entities, none of them reporting) means the pairing step was missed, not that the
> integration failed to find your devices. Delete the config entry and re-add it, timing a
> fresh Connect press to the prompt.

### PIN-based gateways

1. Select **PIN** when prompted

2. Enter the 6-digit PIN from the gateway sticker

3. **Before initial setup/pairing, temporarily disable ESPHome Bluetooth proxies** so Home Assistant is forced to pair through the host's internal/direct Bluetooth adapter

4. **Requires a direct USB Bluetooth adapter** — see [PIN Gateway Requirements](#pin-gateway-requirements) below

## PIN Gateway Requirements

PIN-based gateways can require a BLE passkey exchange during bonding that requires direct access to the host's BlueZ Bluetooth stack.

### Direct USB Bluetooth adapter (supported)

The integration registers a BlueZ D-Bus agent that provides the passkey automatically during bonding. No manual steps required beyond entering the PIN in the config flow. This path is confirmed working.

What matters is reaching the host's BlueZ stack over D-Bus, not running on bare metal. A
containerised Home Assistant works provided the host's D-Bus socket is proxied in — a Core
install in an Incus/LXC container on Ubuntu, using the host's built-in Intel AX211 adapter
through a D-Bus socket proxy, is confirmed working with no USB dongle and no Bluetooth proxy.

**Compatible hardware:** the Raspberry Pi's built-in Bluetooth adapter, or any USB Bluetooth dongle recognized by the HA host. To extend range as much as possible, a USB adapter with an antenna is highly recommended.

### ESPHome Bluetooth Proxy (not supported for PIN gateways)

ESPHome proxies forward GATT operations but do not relay BLE passkey/pairing events back to the HA host. The passkey exchange must happen on the device with the BLE radio, which is the ESP32 — but the ESP32 has no way to receive the PIN from HA during a live pairing attempt.

**Push-to-Pair gateways work normally through ESPHome proxies.** Only PIN gateways are affected.

### Experimental: pre-bond the ESP32 to the gateway

It is possible to bond an ESP32 proxy device directly to the gateway before deploying it as a proxy. The bond is stored in the ESP32's NVS flash and survives OTA firmware updates (as long as flash is not erased). Once bonded, the proxy can connect to the gateway without a passkey exchange, and the integration handles application-layer authentication as normal.

This approach is experimental and has not yet been field-validated end to end — if you try it, please report results (success or failure) on the [issue tracker](https://github.com/phurth/ha-onecontrol/issues). If you are attempting this, use the [`docs/pairing_test.yml`](docs/pairing_test.yml) ESPHome configuration; the full step-by-step procedure is in that file's header comments. Key requirements:

- Both the pairing helper firmware and the production proxy firmware must use the **Bluedroid** BLE stack (not NimBLE) — bond storage is not compatible between the two stacks. Bluedroid is currently the ESPHome default, but keep the helper's `sdkconfig_options` block in the production proxy config as insurance.

- Flash the pairing helper to the **exact device** that will serve as the proxy — bonds are not transferable between ESP32 units

- The BLE passkey must be entered as an **integer** (a sticker PIN of "012345" is `passkey: 12345` — leading zeros are stripped; YAML would otherwise parse it as octal). The leading zero *does* still matter for the PIN entered in the integration's config flow.

- OTA-flash the production proxy firmware **without erasing flash** after bonding

- On HA 2026.6+, check the proxy's Bluetooth scanner mode: the "Auto" default can leave proxies passive-only when a local adapter is also present. If the gateway isn't discovered or connections fail, set the proxy's Scanning mode to **Active** in the Bluetooth integration options.

## Confirmed gateways

Models community members have reported working, and how they pair:

| Gateway | Pairing | Notes |
| - | - | - |
| **Unity X270L** (27478-N) | Push-to-Pair + sticker PIN | Connect button is on-screen on the touchscreen panel ([#11](https://github.com/phurth/ha-onecontrol/issues/11)) |
| **Unity X180T** | Push-to-Pair | IDS-CAN over BLE ([#8](https://github.com/phurth/ha-onecontrol/issues/8)) |
| **Unity X1.5** | Either — ships in both variants | If PIN setup fails, re-add as Push-to-Pair ([#9](https://github.com/phurth/ha-onecontrol/issues/9)) |

This list is not exhaustive — other OneControl gateways are expected to work. If yours does,
opening an issue to say so helps fill this table in.

### What will not appear

Only equipment on the Lippert bus is visible to the gateway, so anything the OneControl panel
and the official app cannot control will not show up in Home Assistant either. Third-party
appliances are the usual case — Coleman-Mach thermostats, for example, are wired independently
and produce no climate entities. If a device is missing here but also absent from the Lippert
panel, that is the hardware layout rather than an integration fault.

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

## Screenshots
Gateway Device
<img width="2036" height="642" alt="image" src="https://github.com/user-attachments/assets/7df9fafc-f233-48e0-897c-bfcb450eebf3" />
Example Entities
<img width="1344" height="2106" alt="image" src="https://github.com/user-attachments/assets/156051c5-c65b-4912-ba77-1e6ec49b15dc" />
Additional Example
<img width="877" height="2662" alt="image" src="https://github.com/user-attachments/assets/3f133e11-d92e-4475-82d8-2a4ebf8f61e6" />

## License

MIT

