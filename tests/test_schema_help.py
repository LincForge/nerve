"""Every `nerve schema --json` option carries help text (reactor d4d13f09, part 1).

linc-cli-kit fills each generated option's help from the tool's JSON-schema property
``description``. Those descriptions come from each tool's Google-style ``Args:`` docstring
section, parsed once at registration (``mcp/_docstring_args.py``). Three guards:

1. the CLI schema has no empty option help;
2. every registered tool's ``Args:`` section names exactly its parameters (drift guard);
3. the MCP inputSchema, ``description`` keys stripped, is byte-identical to origin/main's
   registration (snapshot in ``tests/snapshots/tool_input_schemas.json``; pin).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from linc_cli_kit import registry_of

from linc_nerve.cli.main import _registry_server, main

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")

_SNAPSHOT = Path(__file__).parent / "snapshots" / "tool_input_schemas.json"


def _schema() -> dict[str, Any]:
    result = CliRunner().invoke(main, ["schema", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _strip_descriptions(node: Any, in_properties: bool = False) -> Any:
    """Drop every ``description`` key, but never a *property* that happens to be named so."""
    if isinstance(node, dict):
        return {
            key: _strip_descriptions(value, key == "properties" and not in_properties)
            for key, value in node.items()
            if in_properties or key != "description"
        }
    if isinstance(node, list):
        return [_strip_descriptions(value) for value in node]
    return node


def _wire_tools() -> list[Any]:
    return asyncio.run(_registry_server().list_tools())


# ---------------------------------------------------------------------------
# 1. CLI: every option of every command has help
# ---------------------------------------------------------------------------


def test_every_schema_option_has_help() -> None:
    commands = _schema()["commands"]
    empty = [
        f"{name} {spec['flag']}"
        for name, command in commands.items()
        for spec in command["options"].values()
        if not (spec.get("help") or "").strip()
    ]
    assert empty == [], f"{len(empty)} options without help: {empty}"


def test_schema_covers_all_commands_and_options() -> None:
    commands = _schema()["commands"]
    options = sum(len(command["options"]) for command in commands.values())
    assert (len(commands), options) == (52, 113)


# ---------------------------------------------------------------------------
# 2. Registration: Args sections match parameters; descriptions reach the schema
# ---------------------------------------------------------------------------


def test_args_section_names_exactly_the_parameters() -> None:
    from linc_nerve.mcp._docstring_args import parse_args_section

    drift = {}
    for spec in registry_of(_registry_server()):
        params = set((spec.parameters.get("properties") or {}).keys())
        documented = set(parse_args_section(spec.fn.__wrapped__.__doc__))
        if params != documented:
            drift[spec.name] = {
                "undocumented": sorted(params - documented),
                "unknown": sorted(documented - params),
            }
    assert drift == {}


def test_every_input_schema_property_has_the_args_description() -> None:
    from linc_nerve.mcp._docstring_args import parse_args_section

    by_name = {spec.name: spec for spec in registry_of(_registry_server())}
    for tool in _wire_tools():
        documented = parse_args_section(by_name[tool.name].fn.__wrapped__.__doc__)
        for param, prop in tool.inputSchema.get("properties", {}).items():
            assert prop.get("description") == documented[param], f"{tool.name}.{param}"
            assert prop["description"].strip(), f"{tool.name}.{param}"


# ---------------------------------------------------------------------------
# 3. Pin: the inputSchema minus descriptions is unchanged from origin/main
# ---------------------------------------------------------------------------


def test_input_schema_unchanged_apart_from_descriptions() -> None:
    snapshot = json.loads(_SNAPSHOT.read_text())
    live = {tool.name: _strip_descriptions(tool.inputSchema) for tool in _wire_tools()}
    assert sorted(live) == sorted(snapshot)
    for name, expected in snapshot.items():
        # json.dumps without sort_keys: key ORDER is part of byte-identity.
        assert json.dumps(live[name]) == json.dumps(expected), name


def test_strip_descriptions_keeps_a_property_named_description() -> None:
    schema = {
        "description": "drop",
        "properties": {"description": {"type": "string", "description": "drop"}},
    }
    assert _strip_descriptions(schema) == {"properties": {"description": {"type": "string"}}}


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


def test_parse_joins_continuation_lines_and_stops_at_next_section() -> None:
    from linc_nerve.mcp._docstring_args import parse_args_section

    doc = """Summary line.

    USE FOR: things.
        Indented continuation that is not an arg.

    Args:
        device: Device serial.
        extras: Optional extras.
            Values are type-dispatched,
            one per line.

        count (int): Typed entry, after a blank line.

    Returns:
        retval: not an argument.
    """
    assert parse_args_section(doc) == {
        "device": "Device serial.",
        "extras": "Optional extras. Values are type-dispatched, one per line.",
        "count": "Typed entry, after a blank line.",
    }


@pytest.mark.parametrize("doc", [None, "", "Summary only.\n\n    Returns:\n        x: y\n"])
def test_parse_without_args_section_is_empty(doc: str | None) -> None:
    from linc_nerve.mcp._docstring_args import parse_args_section

    assert parse_args_section(doc) == {}


def test_parse_ignores_text_before_the_first_entry() -> None:
    from linc_nerve.mcp._docstring_args import parse_args_section

    doc = "S.\n\nArgs:\n    not an entry line\n    x: X value.\n"
    # The first indented line fixes the entry column; a non-``name:`` line there that
    # precedes any entry has nothing to continue and is dropped.
    assert parse_args_section(doc) == {"x": "X value."}


def test_described_signature_annotates_only_documented_typed_params() -> None:
    from typing import Annotated, get_args, get_origin

    from linc_nerve.mcp._docstring_args import described_signature

    def fn(device: str, x, y: int = 3) -> dict:  # type: ignore[no-untyped-def]
        """Do it.

        Args:
            device: Device serial.
            x: Untyped.
        """

    sig = described_signature(fn)
    device, x, y = sig.parameters.values()
    assert get_origin(device.annotation) is Annotated
    base, field = get_args(device.annotation)
    assert base is str and field.description == "Device serial."
    assert x.annotation is x.empty  # unannotated: left alone (FastMCP's own fallback)
    assert y.annotation is int and y.default == 3  # undocumented: left alone
    assert sig.return_annotation is dict
