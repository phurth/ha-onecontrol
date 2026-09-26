"""Tests for the cover safety-stop timeout option.

The safety timeout started as a hard-coded module constant after a runaway
awning event.  It is now a per-entry option (``cover_safety_timeout``) so users
can tune the fail-safe duration.  These tests pin the three places that must
stay in agreement: the option key/default in ``const.py``, the options-flow
form field in ``config_flow.py``, and the coordinator's per-instance read in
``coordinator.py``.

Modules are read as source (AST) rather than imported: the config flow
subclasses Home Assistant's ``ConfigFlow``, which this harness stubs with a
MagicMock, so importing raises at class-definition time.
"""

import ast
from pathlib import Path

COMPONENT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "ha_onecontrol"
)


def _parse(name: str) -> ast.Module:
    """Parse one of the integration's modules into an AST."""
    return ast.parse((COMPONENT / name).read_text())


def _consts() -> dict[str, object]:
    """Return module-level constant assignments from const.py."""
    out: dict[str, object] = {}
    for node in ast.walk(_parse("const.py")):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value if isinstance(node.value, ast.Constant) else None
    return out


def _options_flow_schema_keys() -> set[str]:
    """Return the CONF_* keys the options-flow schema requires."""
    keys: set[str] = set()
    for node in ast.walk(_parse("config_flow.py")):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Required"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Name):
                    keys.add(arg.id)
    return keys


def test_safety_timeout_option_key_and_default_defined() -> None:
    """The option key and a 6-second default must exist in const.py."""
    consts = _consts()
    assert consts.get("CONF_COVER_SAFETY_TIMEOUT") == "cover_safety_timeout"
    assert consts.get("DEFAULT_COVER_SAFETY_TIMEOUT") == 6.0


def test_options_flow_presents_safety_timeout_field() -> None:
    """The options flow must expose the safety-timeout field."""
    assert "CONF_COVER_SAFETY_TIMEOUT" in _options_flow_schema_keys()


def test_coordinator_reads_safety_timeout_from_options() -> None:
    """The coordinator must read the option, defaulting to 6 seconds."""
    text = (COMPONENT / "coordinator.py").read_text()
    assert "self._cover_safety_timeout_s" in text
    assert "CONF_COVER_SAFETY_TIMEOUT" in text
    assert "DEFAULT_COVER_SAFETY_TIMEOUT" in text
    # The repeaters must gate on the per-instance value, not a stale constant.
    assert "_COVER_SAFETY_TIMEOUT_S" not in text
