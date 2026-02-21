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

**Legacy PIN gateways (older):**
- Enter the PIN when prompted (used for both BLE bonding and protocol auth)

## Supported Devices

- System voltage sensor
- System temperature sensor
- Relay switches (lights, appliances)
- Tank level sensors
- Dimmable lights
- HVAC climate zones
- Covers/slides (H-Bridge)

## Development

### Deploy to HA instance

```bash
./scripts/deploy.sh
```

### Run tests

```bash
pip install -r requirements_test.txt
pytest tests/
```

## License

MIT
