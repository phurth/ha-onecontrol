"""Shared test fixtures.

Stubs out homeassistant and related packages so the integration's modules can
be imported without a full Home Assistant install.

``DataUpdateCoordinator`` must be a real class (the coordinator subclasses it),
and ``callback`` must behave as a pass-through decorator (it decorates methods
at class-definition time).  Everything else is a lightweight stand-in that
auto-resolves missing attributes to MagicMocks.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class DataUpdateCoordinator:
    """Minimal stand-in base class for OneControlCoordinator."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    @classmethod
    def __class_getitem__(cls, item: object) -> type:
        return cls


def _callback(fn: object) -> object:
    """Pass-through decorator, matching homeassistant.core.callback."""
    return fn


class _AutoMockModule(types.ModuleType):
    """A module whose missing attributes resolve to MagicMocks on access."""

    def __getattr__(self, name: str) -> object:
        if name.startswith("__"):
            raise AttributeError(name)
        value = MagicMock()
        setattr(self, name, value)
        return value


def _stub(name: str) -> types.ModuleType:
    mod = _AutoMockModule(name)
    sys.modules[name] = mod
    return mod


# Attributes exercised at import / class-definition time get explicit values.
_stub("homeassistant")
_stub("homeassistant.core").callback = _callback
_stub("homeassistant.const").CONF_ADDRESS = "address"
_stub("homeassistant.helpers.update_coordinator").DataUpdateCoordinator = DataUpdateCoordinator

for _name in [
    "homeassistant.config_entries",
    "homeassistant.helpers",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.entity_platform",
    "homeassistant.components",
    "homeassistant.components.bluetooth",
    "homeassistant.components.sensor",
    "homeassistant.components.number",
    "homeassistant.components.button",
    "homeassistant.components.cover",
    "voluptuous",
    "bleak",
    "bleak.exc",
    "bleak.uuids",
    "bleak_retry_connector",
]:
    _stub(_name)
