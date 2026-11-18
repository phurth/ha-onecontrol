"""Per-zone climate settings: allowed HVAC modes and setpoint range.

Issue #20: every zone used to offer the same five modes and a fixed 40–95 °F
range, which is wrong for a zone with no heat source or a thermostat with a
higher floor.  Neither can be detected reliably, so the options flow lets the
user narrow them per zone; a zone without stored settings is unchanged.

Covers the pure settings logic, the climate entity reading it (through the
simulated gateway from ``test_device_identity``), and the options-flow form.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

from custom_components.ha_onecontrol.climate_zones import (
    DEFAULT_ZONE_SETTINGS,
    ZoneSettings,
    stored_zone_overrides,
    zone_override,
    zone_settings,
)
from custom_components.ha_onecontrol.protocol.events import HvacZone

from .test_device_identity import TABLE_DB, TABLE_DC, Rig, platforms  # noqa: F401

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "ha_onecontrol"
MAIN = "fn00c0_00"  # Main Climate Zone: function 192, instance 0


# ── Settings logic ────────────────────────────────────────────────────────


def test_zone_without_settings_keeps_todays_behaviour() -> None:
    assert DEFAULT_ZONE_SETTINGS == ZoneSettings(("heat", "cool", "heat_cool", "auto"), 40, 95)
    for options in (None, {}, {"climate_zones": {}}, {"climate_zones": {"other": {"modes": []}}}):
        assert zone_settings(options, MAIN) == DEFAULT_ZONE_SETTINGS
    assert zone_settings({"climate_zones": {MAIN: {"modes": []}}}, None) == DEFAULT_ZONE_SETTINGS


def test_stored_settings_are_applied() -> None:
    options = {
        "climate_zones": {
            MAIN: {"modes": ["heat_cool", "cool", "heat"], "min_temp": 47, "max_temp": 90},
            "fn00c1_00": {"modes": ["cool"], "min_temp": 40, "max_temp": 95},
            "fn00c2_00": {"modes": [], "min_temp": 60, "max_temp": 80},
        }
    }
    # Modes come back in display order, whatever order they were stored in.
    assert zone_settings(options, MAIN) == ZoneSettings(("heat", "cool", "heat_cool"), 47, 90)
    assert zone_settings(options, "fn00c1_00") == ZoneSettings(("cool",), 40, 95)
    # No modes at all is allowed: the zone can only be switched off.
    assert zone_settings(options, "fn00c2_00") == ZoneSettings((), 60, 80)


def test_malformed_settings_never_break_a_zone() -> None:
    def resolve(raw: object) -> ZoneSettings:
        return zone_settings({"climate_zones": {MAIN: raw}}, MAIN)

    assert zone_settings({"climate_zones": "nonsense"}, MAIN) == DEFAULT_ZONE_SETTINGS
    assert resolve("nonsense") == DEFAULT_ZONE_SETTINGS
    assert resolve({}) == DEFAULT_ZONE_SETTINGS
    # Unknown modes are dropped; a non-list falls back to every mode.
    assert resolve({"modes": ["cool", "dry", 7]}).modes == ("cool",)
    assert resolve({"modes": "cool"}).modes == DEFAULT_ZONE_SETTINGS.modes
    # Limits are clamped to 40–95 and non-numbers fall back to the default.
    assert resolve({"min_temp": 10, "max_temp": 150}) == DEFAULT_ZONE_SETTINGS
    assert resolve({"min_temp": "47", "max_temp": None}) == ZoneSettings(min_temp=47)
    assert resolve({"min_temp": True, "max_temp": "hot"}) == DEFAULT_ZONE_SETTINGS
    # An inverted or empty range is ignored rather than making setpoints impossible.
    assert resolve({"min_temp": 80, "max_temp": 60}) == DEFAULT_ZONE_SETTINGS
    assert resolve({"min_temp": 70, "max_temp": 70}) == DEFAULT_ZONE_SETTINGS


def test_default_settings_store_nothing() -> None:
    assert zone_override(DEFAULT_ZONE_SETTINGS) is None
    settings = ZoneSettings(("heat", "cool", "heat_cool"), 47, 95)
    override = zone_override(settings)
    assert override == {
        "modes": ["heat", "cool", "heat_cool"], "min_temp": 47, "max_temp": 95, "heat_sources": [],
    }
    json.dumps(override)  # must be storable in the config entry
    assert zone_settings({"climate_zones": {MAIN: override}}, MAIN) == settings
    # Declaring a heat source is a setting too, even with everything else default.
    only_sources = ZoneSettings(heat_sources=("gas", "heat_pump"))
    assert zone_override(only_sources)["heat_sources"] == ["gas", "heat_pump"]
    assert zone_settings({"climate_zones": {MAIN: zone_override(only_sources)}}, MAIN) == only_sources


def test_declared_heat_sources() -> None:
    def resolve(raw: object) -> ZoneSettings:
        return zone_settings({"climate_zones": {MAIN: {"heat_sources": raw}}}, MAIN)

    assert DEFAULT_ZONE_SETTINGS.heat_sources == () and DEFAULT_ZONE_SETTINGS.capability == 0
    assert resolve(["heat_pump", "gas"]).heat_sources == ("gas", "heat_pump")
    assert resolve(["gas"]).capability == 0x01
    assert resolve(["heat_pump"]).capability == 0x04
    assert resolve(["gas", "heat_pump"]).capability == 0x05
    # Unknown names and malformed values declare nothing.
    assert resolve(["electric", 3]).heat_sources == ()
    assert resolve("gas").heat_sources == () == resolve(None).heat_sources


def test_stored_overrides_are_copied() -> None:
    options = {"climate_zones": {MAIN: {"modes": ["cool"]}, 5: {}, "bad": "x"}}
    copy = stored_zone_overrides(options)
    assert copy == {MAIN: {"modes": ["cool"]}}
    copy[MAIN]["modes"] = []
    assert options["climate_zones"][MAIN] == {"modes": ["cool"]}
    assert stored_zone_overrides(None) == {} == stored_zone_overrides({"climate_zones": []})


# ── Climate entity ────────────────────────────────────────────────────────


def _climate_rig(platforms: dict, options: dict) -> tuple[Rig, object, object]:  # noqa: F811
    rig = Rig(platforms)
    rig.coordinator.entry = SimpleNamespace(entry_id="e1", options=options)
    rig.boot(0xDB, TABLE_DB)
    return rig, rig.entity("climate.main_climate_zone"), platforms["climate"].HVACMode


def _set_mode(rig: Rig, entity: object, heat_mode: int) -> None:
    key = entity._key
    rig.coordinator.hvac_zones[key] = HvacZone(
        table_id=entity._table_id, device_id=entity._device_id, heat_mode=heat_mode
    )


def test_unconfigured_zone_offers_every_mode(platforms) -> None:  # noqa: F811
    _rig, main, mode = _climate_rig(platforms, {})
    assert main.hvac_modes == [mode.OFF, mode.HEAT, mode.COOL, mode.HEAT_COOL, mode.AUTO]
    assert (main.min_temp, main.max_temp) == (40, 95)


def test_configured_zone_offers_only_its_modes_and_range(platforms) -> None:  # noqa: F811
    options = {
        "climate_zones": {
            MAIN: {"modes": ["heat", "cool", "heat_cool"], "min_temp": 47, "max_temp": 95}
        }
    }
    rig, main, mode = _climate_rig(platforms, options)
    assert main._oc_ident == MAIN
    assert main.hvac_modes == [mode.OFF, mode.HEAT, mode.COOL, mode.HEAT_COOL]
    assert (main.min_temp, main.max_temp) == (47, 95)

    # Options are read live: a change applies without recreating the entity.
    options["climate_zones"][MAIN] = {"modes": ["cool"], "min_temp": 60, "max_temp": 80}
    assert main.hvac_modes == [mode.OFF, mode.COOL]
    assert (main.min_temp, main.max_temp) == (60, 80)
    del options["climate_zones"]
    assert len(main.hvac_modes) == 5 and main.min_temp == 40


def test_reported_mode_is_always_listed(platforms) -> None:  # noqa: F811
    """Set to Heat from the coach's own controls: the state must stay valid."""
    options = {"climate_zones": {MAIN: {"modes": ["cool"]}}}
    rig, main, mode = _climate_rig(platforms, options)
    _set_mode(rig, main, 1)
    assert main.hvac_mode == mode.HEAT
    assert main.hvac_modes == [mode.OFF, mode.COOL, mode.HEAT]
    _set_mode(rig, main, 2)
    assert main.hvac_modes == [mode.OFF, mode.COOL]


def test_settings_follow_the_zone_through_a_renumbering(platforms) -> None:  # noqa: F811
    options = {"climate_zones": {MAIN: {"modes": ["heat"], "min_temp": 47}}}
    rig, main, mode = _climate_rig(platforms, options)
    before = (main._table_id, main._device_id)

    rig.gateway(0xDC)
    rig.commit_metadata(0xDC, TABLE_DC)
    rig.status(0xDC, TABLE_DC)

    assert (main._table_id, main._device_id) != before
    assert main.hvac_modes == [mode.OFF, mode.HEAT] and main.min_temp == 47
    # The other zones were never configured and now sit on different rows too.
    bedroom = rig.entity("climate.bedroom_climate_zone")
    assert len(bedroom.hvac_modes) == 5 and bedroom.min_temp == 40


def test_declared_heat_sources_offer_their_presets(platforms) -> None:  # noqa: F811
    """A zone with gas and a heat pump that has only ever used gas."""
    options: dict = {}
    rig, main, _mode = _climate_rig(platforms, options)
    c = rig.coordinator
    # Nothing learned, nothing declared: no presets (today's behaviour).
    assert main.preset_modes is None
    # Learned gas only: the heat pump preset is missing until it has been used.
    c.observed_hvac_capability[main._key] = 0x01
    assert main.preset_modes == ["none", "Prefer Gas"]

    options["climate_zones"] = {MAIN: {"heat_sources": ["heat_pump"]}}
    assert main.preset_modes == ["none", "Prefer Gas", "Prefer Heat Pump"]
    # Declared sources only add: with nothing learned, the declared one shows…
    c.observed_hvac_capability.clear()
    assert main.preset_modes == ["none", "Prefer Heat Pump"]
    # …and a learned one is never hidden by leaving its box unticked.
    c.observed_hvac_capability[main._key] = 0x05
    options["climate_zones"] = {MAIN: {"heat_sources": []}}
    assert main.preset_modes == ["none", "Prefer Gas", "Prefer Heat Pump"]
    # Other zones are unaffected.
    assert rig.entity("climate.bedroom_climate_zone").preset_modes is None


def test_address_keyed_install_uses_the_address_key(platforms) -> None:  # noqa: F811
    """IDS-CAN BLE: no function ID, so settings are keyed by "tt:dd"."""
    rig = Rig(platforms)
    c = rig.coordinator
    c.entry = SimpleNamespace(entry_id="e1", options={"climate_zones": {"00:86": {"modes": ["cool"]}}})
    rig._emit(c.hvac_zones, HvacZone(table_id=0, device_id=0x86))
    assert c.identity_mode == "legacy"
    (zone,) = rig.platforms["climate"].loaded.values()
    assert zone._oc_ident is None
    mode = platforms["climate"].HVACMode
    assert zone.hvac_modes == [mode.OFF, mode.COOL]


# ── Options flow ──────────────────────────────────────────────────────────


def _parse(name: str) -> ast.Module:
    return ast.parse((COMPONENT / name).read_text(encoding="utf-8"))


def _consts() -> dict[str, object]:
    out: dict[str, object] = {}
    for node in _parse("const.py").body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def test_zone_form_fields_have_translations() -> None:
    consts = _consts()
    fields = {
        consts[name]
        for name in (
            "CONF_ZONE_ALLOW_HEAT", "CONF_ZONE_ALLOW_COOL", "CONF_ZONE_ALLOW_HEAT_COOL",
            "CONF_ZONE_ALLOW_AUTO", "CONF_ZONE_MIN_TEMP", "CONF_ZONE_MAX_TEMP",
            "CONF_ZONE_HAS_GAS", "CONF_ZONE_HAS_HEAT_PUMP",
        )
    }
    assert len(fields) == 8
    for path in (COMPONENT / "strings.json", COMPONENT / "translations" / "en.json"):
        options = json.loads(path.read_text(encoding="utf-8"))["options"]
        step = options["step"]["climate_zone"]
        assert set(step["data"]) == fields, path
        for placeholder in ("{name}", "{index}", "{total}"):
            assert placeholder in step["description"], path
        assert consts["CONF_CONFIGURE_CLIMATE_ZONES"] in options["step"]["init"]["data"], path
        assert "zone_min_not_below_max" in options["error"], path


def test_option_texts_use_real_line_breaks() -> None:
    """A doubly escaped newline shows up in the dialog as a literal "\\n"."""
    for path in (COMPONENT / "strings.json", COMPONENT / "translations" / "en.json"):
        steps = json.loads(path.read_text(encoding="utf-8"))["options"]["step"]
        for name, step in steps.items():
            assert "\\n" not in step["description"], (path.name, name)
        assert "\n\n" in steps["init"]["description"], path.name


def test_zone_temp_field_serialises_for_the_frontend() -> None:
    """A schema the frontend cannot serialise means the page never appears."""
    script = textwrap.dedent(
        """
        import ast
        import sys

        import voluptuous as vol
        from voluptuous_serialize import convert

        tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
        value = next(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and target.id == "_ZONE_TEMP_FIELD"
        )
        names = {"vol": vol, "DEFAULT_HVAC_MIN_TEMP_F": 40, "DEFAULT_HVAC_MAX_TEMP_F": 95}
        validator = eval(compile(ast.fix_missing_locations(ast.Expression(body=value)), "<schema>", "eval"), names)
        schema = vol.Schema({vol.Required("allow_heat", default=True): bool, vol.Required("min_temp", default=40): validator})
        convert(schema)
        assert schema({"allow_heat": False, "min_temp": "47"}) == {"allow_heat": False, "min_temp": 47}
        for bad in (39, 96, "x"):
            try:
                schema({"allow_heat": True, "min_temp": bad})
            except vol.Invalid:
                continue
            raise AssertionError(bad)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(COMPONENT / "config_flow.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_saving_zone_settings_does_not_reload_the_entry() -> None:
    """Zone settings are read live; a reload would drop the gateway connection."""
    tree = _parse("__init__.py")
    snapshot = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_reload_snapshot"
    )
    names = {node.id for node in ast.walk(snapshot) if isinstance(node, ast.Name)}
    assert {"CONF_BONDED_SOURCE", "CONF_CLIMATE_ZONES"} <= names
    source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    # Saving merges onto the existing options and drops the key when empty.
    assert "**self.config_entry.options" in source
    assert "options.pop(CONF_CLIMATE_ZONES, None)" in source
