"""Tests for the config-entry version invariant.

Home Assistant refuses to load an entry whose stored version is higher than the
flow handler's ``VERSION``.  Releases 1.0.39-1.0.45 shipped a migration ladder
that walked entries to 4 while the handler still declared 2, which stranded
every install whose entry predated version 2: the first boot after upgrading
migrated the entry to 4, and every boot after that refused to load it with
"has version 4 which is higher than the current version 2".

The modules are read as source rather than imported: the config flow subclasses
Home Assistant's ``ConfigFlow``, which this test harness stubs with a MagicMock,
so importing it raises at class-definition time.
"""

import ast
from pathlib import Path

COMPONENT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "ha_onecontrol"
)


def _parse(name: str) -> ast.Module:
    """Parse one of the integration's modules into an AST."""
    return ast.parse((COMPONENT / name).read_text())


def _flow_version() -> int:
    """Return the VERSION declared by the config flow handler."""
    for node in ast.walk(_parse("config_flow.py")):
        if isinstance(node, ast.ClassDef) and node.name == "OneControlConfigFlow":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "VERSION"
                    for target in stmt.targets
                ):
                    assert isinstance(stmt.value, ast.Constant)
                    return stmt.value.value
    raise AssertionError("OneControlConfigFlow.VERSION not found")


def _migration_versions() -> set[int]:
    """Return every entry version async_migrate_entry writes."""
    for node in ast.walk(_parse("__init__.py")):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "async_migrate_entry"
        ):
            return {
                keyword.value.value
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                for keyword in call.keywords
                if keyword.arg == "version" and isinstance(keyword.value, ast.Constant)
            }
    raise AssertionError("async_migrate_entry not found")


def test_flow_version_matches_migration_ladder() -> None:
    """The handler must declare the highest version the ladder can produce.

    If the ladder runs ahead, migrated entries load once and then fail on every
    subsequent restart — and reverting to an older release makes it worse, since
    that declares an even lower VERSION.
    """
    versions = _migration_versions()
    assert versions, "migration ladder writes no entry versions"
    assert _flow_version() == max(versions)


def test_migration_ladder_has_no_gaps() -> None:
    """Every step from the first migration to the last must exist.

    A gap would leave entries sitting on a version no branch picks up, which
    strands them exactly the way an over-running ladder does.
    """
    versions = _migration_versions()
    assert sorted(versions) == list(range(min(versions), max(versions) + 1))
