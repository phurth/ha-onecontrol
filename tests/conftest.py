"""Shared test fixtures.

Stubs optional dependencies only when they are unavailable in the active
environment so tests can run both with and without Home Assistant installed.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── Stub out homeassistant and friends so __init__.py can be imported ────


class _StubModule(MagicMock):
    """A MagicMock that acts as a module for `from X import Y` support."""

    def __repr__(self) -> str:
        return f"<StubModule {self._mock_name!r}>"


_STUBS = [
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.const",
    "homeassistant.helpers",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity_platform",
    "homeassistant.components",
    "homeassistant.components.bluetooth",
    "homeassistant.components.sensor",
    "voluptuous",
    "bleak",
    "bleak.backends",
    "bleak.backends.characteristic",
    "bleak.exc",
]

for _name in _STUBS:
    try:
        importlib.import_module(_name)
    except Exception:  # noqa: BLE001
        sys.modules.setdefault(_name, _StubModule(name=_name))


# Provide minimal concrete symbols required by coordinator imports when
# Home Assistant is not installed and modules are stubbed.
_ha_core = sys.modules.get("homeassistant.core")
if isinstance(_ha_core, _StubModule):
    class _HomeAssistant:  # pragma: no cover - test shim
        pass

    def _callback(func):  # pragma: no cover - test shim
        return func

    _ha_core.HomeAssistant = _HomeAssistant
    _ha_core.callback = _callback

_ha_cfg = sys.modules.get("homeassistant.config_entries")
if isinstance(_ha_cfg, _StubModule):
    class _ConfigEntry:  # pragma: no cover - test shim
        pass

    _ha_cfg.ConfigEntry = _ConfigEntry

_ha_uc = sys.modules.get("homeassistant.helpers.update_coordinator")
if isinstance(_ha_uc, _StubModule):
    class _DataUpdateCoordinator:  # pragma: no cover - test shim
        def __init__(self, hass, logger, name=None, update_interval=None, always_update=False):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.always_update = always_update
            self.data = {}

        def async_set_updated_data(self, data):
            self.data = data

    _ha_uc.DataUpdateCoordinator = _DataUpdateCoordinator
