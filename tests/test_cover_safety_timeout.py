"""Tests for the cover travel-time / safety-ceiling model.

The safety stop started as a hard-coded module constant after a runaway awning
event, then became a single per-entry option.  It is now split into two distinct
notions so a multi-cover coach can tune each cover independently:

* a fallback **travel time** (option ``cover_safety_timeout``, default 6.0 s) —
  how long one open/close press runs the motor, and
* a **derived safety ceiling** — the failsafe bound on the repeater, never
  entered by hand — enforced OUTSIDE the repeater loop via ``asyncio.timeout``.

These tests pin the places that must stay in agreement: the option keys and
default in ``const.py``, the options-flow form fields in ``config_flow.py``, and
the coordinator's per-instance read and derived-ceiling helper.

Modules are read as source (AST) rather than imported: the config flow
subclasses Home Assistant's ``ConfigFlow``, which this harness stubs with a
MagicMock, so importing raises at class-definition time.
"""

import ast
import sys
from pathlib import Path

import pytest

COMPONENT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "ha_onecontrol"
)
sys.path.insert(0, str(COMPONENT.parent.parent))


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
    """Return the CONF_* keys the options-flow schema references (by name)."""
    keys: set[str] = set()
    for node in ast.walk(_parse("config_flow.py")):
        if isinstance(node, ast.Name):
            keys.add(node.id)
    return keys


def test_travel_time_option_key_and_default_defined() -> None:
    """The fallback travel option and a 6-second default must exist in const.py."""
    consts = _consts()
    assert consts.get("CONF_COVER_SAFETY_TIMEOUT") == "cover_safety_timeout"
    assert consts.get("DEFAULT_COVER_SAFETY_TIMEOUT") == 6.0
    assert consts.get("CONF_COVER_TRAVEL") == "cover_travel"


def test_safety_ceiling_constants_defined() -> None:
    """The derived-ceiling clamp and cap constants must exist in const.py."""
    consts = _consts()
    assert consts.get("COVER_CEILING_MIN_PAD_S") == 2.0
    assert consts.get("COVER_CEILING_MAX_PAD_S") == 5.0
    assert consts.get("COVER_CEILING_CAP_S") == 90.0


def test_options_flow_presents_travel_time_field() -> None:
    """The options flow must reference the fallback travel-time key."""
    assert "CONF_COVER_SAFETY_TIMEOUT" in _options_flow_schema_keys()


def test_options_flow_merges_existing_options() -> None:
    """Saving the options form must preserve existing options (bonded_source)."""
    text = (COMPONENT / "config_flow.py").read_text()
    # Must merge onto the current options, never rebuild from a bare dict.
    assert "**self.config_entry.options" in text


def test_coordinator_reads_travel_time_from_options() -> None:
    """The coordinator must read the option, defaulting to 6 seconds."""
    text = (COMPONENT / "coordinator.py").read_text()
    assert "CONF_COVER_SAFETY_TIMEOUT" in text
    assert "DEFAULT_COVER_SAFETY_TIMEOUT" in text
    assert "CONF_COVER_TRAVEL" in text
    # The repeaters must gate on the per-instance value, not a stale constant.
    assert "_COVER_SAFETY_TIMEOUT_S" not in text
    # The failsafe must be enforced outside the loop.
    assert "asyncio.timeout(" in text


def test_cover_task_tracking_keyed_by_cover_key() -> None:
    """Cover tasks and generation must be keyed by the "tt:dd" cover key."""
    text = (COMPONENT / "coordinator.py").read_text()
    assert "self._cover_command_tasks: dict[str, asyncio.Task]" in text
    assert "self._cover_gen: dict[str, int]" in text
    # No leftover device_id-only keying.
    assert "dict[int, asyncio.Task]" not in text


def test_ceiling_formula_matches_spec() -> None:
    """The ceiling helper must implement travel + clamp(0.2*travel, 2, 5), cap 90."""
    from custom_components.ha_onecontrol.coordinator import OneControlCoordinator

    ceiling = OneControlCoordinator._cover_safety_ceiling_s

    # 3 s awning: pad clamps up to 2 s -> 5 s (0.6 s would trip spuriously).
    assert ceiling(3.0) == pytest.approx(5.0)
    # 45 s slide: pad clamps down to 5 s -> 50 s (9 s would be excessive).
    assert ceiling(45.0) == pytest.approx(50.0)
    # 10 s: pad = 2.0 s -> 12 s.
    assert ceiling(10.0) == pytest.approx(12.0)
    # Hard cap at 90 s.
    assert ceiling(100.0) == pytest.approx(90.0)


def test_clamp_travel_time_tolerates_malformed_values() -> None:
    """A malformed or out-of-range stored value must fall back or clamp."""
    from custom_components.ha_onecontrol.coordinator import OneControlCoordinator

    clamp = OneControlCoordinator._clamp_travel_time
    assert clamp(6.0) == 6.0
    assert clamp("30") == 30.0
    assert clamp("garbage") == 6.0
    assert clamp(None) == 6.0
    assert clamp(0.5) == 1.0
    assert clamp(9999) == 60.0
    assert clamp(float("inf")) == 6.0


def test_travel_time_override_resolution() -> None:
    """Per-cover, per-direction overrides beat the fallback, then clamp."""
    from custom_components.ha_onecontrol.coordinator import OneControlCoordinator

    coord = object.__new__(OneControlCoordinator)
    coord._cover_travel_fallback_s = 6.0
    coord._cover_travel_overrides = {
        "00:06": {"extend": 30.0, "retract": 28.0},
        "00:07": {"extend": 25.0, "retract": 25.0},
    }
    # Per-direction override.
    assert coord._cover_travel_time_s("00:06", 0x01) == 30.0
    assert coord._cover_travel_time_s("00:06", 0x02) == 28.0
    # Single-value override applied to both directions.
    assert coord._cover_travel_time_s("00:07", 0x01) == 25.0
    assert coord._cover_travel_time_s("00:07", 0x02) == 25.0
    # No override -> fallback.
    assert coord._cover_travel_time_s("00:08", 0x01) == 6.0


def test_parse_cover_travel_overrides_normalises_shapes() -> None:
    """Malformed override shapes are dropped; scalars expand to both directions."""
    from custom_components.ha_onecontrol.coordinator import OneControlCoordinator

    parse = OneControlCoordinator._parse_cover_travel_overrides
    assert parse(None) == {}
    assert parse("junk") == {}
    assert parse({"00:06": {"extend": 30, "retract": "x"}}) == {"00:06": {"extend": 30.0}}
    assert parse({"00:06": 25}) == {"00:06": {"extend": 25.0, "retract": 25.0}}
    assert parse({123: 25}) == {}


# ---------------------------------------------------------------------------
# Per-cover options-flow form (multi-cover review feedback)
#
# Two regressions were found on a multi-cover rig and both are pinned here:
#   * the per-cover fields were validated by a plain function, which the
#     frontend cannot serialise ("Unable to convert schema"), so the step
#     could not be displayed at all; and
#   * the field names were built from the cover key ("extend_0c:06"), so no
#     translation matched and every field rendered unlabelled.
# ---------------------------------------------------------------------------

import json
import subprocess
import textwrap

_STRINGS = COMPONENT / "strings.json"
_TRANSLATIONS = COMPONENT / "translations" / "en.json"


def _travel_field_values() -> list[ast.expr]:
    """Return the validator expressions used for the per-cover form fields."""
    out: list[ast.expr] = []
    for node in ast.walk(_parse("config_flow.py")):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_COVER_TRAVEL_FIELD":
                out.append(node.value)
    return out


def test_per_cover_schema_values_are_not_bare_functions() -> None:
    """Schema validators must be voluptuous calls, not plain functions.

    Home Assistant serialises the form schema to render it; a bare Python
    function (e.g. a ``@staticmethod`` validator) raises ``Unable to convert
    schema`` in the frontend and the step never appears.
    """
    values = _travel_field_values()
    assert values, "per-cover schema fields not found in config_flow.py"
    for value in values:
        assert isinstance(value, ast.Call), ast.dump(value)[:200]
        assert getattr(value.func, "attr", None) in {"Any", "All"}, ast.dump(value)[:200]


def test_per_cover_schema_serialises_for_the_frontend() -> None:
    """Build the per-cover validators for real and serialise them."""
    script = textwrap.dedent(
        """
        import ast
        import sys

        import voluptuous as vol
        from voluptuous_serialize import convert

        tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
        values = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and target.id == "_COVER_TRAVEL_FIELD"
        ]
        assert values, "no per-cover schema values found"
        for value in values:
            validator = eval(compile(ast.fix_missing_locations(ast.Expression(body=value)), "<schema>", "eval"), {"vol": vol})
            convert(vol.Schema({vol.Optional("field"): validator}))
        print(len(values))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(COMPONENT / "config_flow.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Unable to convert schema" not in result.stderr


def test_per_cover_field_names_have_translations() -> None:
    """Every per-cover field name must exist in the form translations."""
    consts = _consts()
    names = {
        consts.get("CONF_COVER_TRAVEL_EXTEND"),
        consts.get("CONF_COVER_TRAVEL_RETRACT"),
    }
    assert names == {"extend", "retract"}

    for path in (_STRINGS, _TRANSLATIONS):
        data = json.loads(path.read_text(encoding="utf-8"))
        travel = data["options"]["step"]["travel"]
        assert set(travel["data"]) >= names, path
        # The page names the cover instead of embedding its key in field names.
        for placeholder in ("{name}", "{index}", "{total}"):
            assert placeholder in travel["description"], path


def test_per_cover_tick_box_and_merge_keys_present() -> None:
    """The flow must offer the opt-in tick box and preserve stored overrides."""
    consts = _consts()
    assert consts.get("CONF_CONFIGURE_COVER_TRAVEL") == "configure_cover_travel"
    source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    assert "CONF_CONFIGURE_COVER_TRAVEL" in source
    # Saving must merge onto the existing options (bonded_source survives).
    assert "**self.config_entry.options" in source


def test_stored_override_can_be_cleared() -> None:
    """A stored value must be offered as a suggestion, never as a default.

    The frontend leaves a field's key out of ``user_input`` when the user
    empties it, and voluptuous re-applies ``default`` in that case — so a
    stored override could be edited but never cleared.  ``suggested_value``
    pre-fills the box without standing in for a blank submission.
    """
    tree = _parse("config_flow.py")
    travel_step = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_step_travel"
    )
    prefilled = [
        node
        for node in ast.walk(travel_step)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Optional"
        and any(kw.arg == "description" for kw in node.keywords)
    ]
    assert prefilled, "no suggested_value pre-fill found in async_step_travel"

    for call in prefilled:
        assert not any(kw.arg == "default" for kw in call.keywords), ast.dump(call)[:300]
        description = next(kw.value for kw in call.keywords if kw.arg == "description")
        assert isinstance(description, ast.Dict), ast.dump(description)[:300]
        keys = [
            k.value
            for k in description.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        ]
        assert keys == ["suggested_value"], ast.dump(description)[:300]


def test_handoff_keeps_the_session_the_new_command_owns() -> None:
    """A repeater that handed the motor off must not close the session.

    The newer command's repeater is using that session; closing it here would
    force a re-open (up to ~3.5s of movement) and lands a close mid-command.
    """
    tree = _parse("coordinator.py")
    repeater = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_cover_command_repeater"
    )
    finally_block = next(
        node
        for node in ast.walk(repeater)
        if isinstance(node, ast.Try) and node.finalbody
    )
    guarded = [
        node
        for node in ast.walk(finally_block)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.operand, ast.Name)
        and node.test.operand.id == "handed_off"
    ]
    assert guarded, "session release is not guarded by handed_off"
    body = ast.unparse(guarded[0].body)
    assert "_async_close_remote_control_session" in body, body


def test_failsafe_stop_and_start_are_bounded() -> None:
    """The start phase and every failsafe STOP must run under a time bound."""
    consts = _consts()
    assert consts.get("COVER_START_TIMEOUT_S") == 10.0
    assert consts.get("COVER_STOP_TIMEOUT_S") == 2.0

    source = (COMPONENT / "coordinator.py").read_text(encoding="utf-8")
    # The bound is applied in one helper; every failsafe path goes through it.
    assert "async def _can_cover_stop_bounded" in source
    assert "async with asyncio.timeout(COVER_STOP_TIMEOUT_S)" in source
    # No failsafe path may call the unbounded helper directly.
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("await self._can_cover_stop("):
            raise AssertionError(f"unbounded STOP call: {stripped}")


def test_travel_clock_starts_at_first_command() -> None:
    """The repeater must measure travel from the first COMMAND written."""
    source = (COMPONENT / "coordinator.py").read_text(encoding="utf-8")
    assert "COVER_START_TIMEOUT_S" in source
    # Two phases: the start phase arms the clock, then the ceiling is armed.
    assert "The motor is now energised: start the travel clock." in source
    assert "time.monotonic() - started >= travel" in source
