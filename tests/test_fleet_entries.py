"""Exact-value unit tests for ``core/fleet_entries`` (bead linc-nerve-56eg).

The end-to-end fleet tests assert policy outcomes; these pin the exact tuples and
counters each helper returns, so a mutated return value or counter cannot hide
behind an outcome that happens to stay the same.
"""

from __future__ import annotations

import datetime

import pytest
import yaml

import linc_nerve.core.ios  # noqa: F401  (conftest imports ios_discovery, which needs ios first)
from linc_nerve.core import fleet_entries
from linc_nerve.core.fleet_entries import (
    FleetAccumulator,
    FleetEntry,
    FleetSafeLoader,
    collect_mapping,
    collect_section,
    load_fleet_yaml,
)

# ---------------------------------------------------------------------------
# _flow_depth_exceeds: exact boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 4, 256])
def test_contract_flow_depth_limit_is_inclusive(limit: int) -> None:
    """[contract] Nesting exactly *limit* deep passes; one more fails."""
    assert fleet_entries._flow_depth_exceeds("[" * limit, limit) is False
    assert fleet_entries._flow_depth_exceeds("{" * limit, limit) is False
    assert fleet_entries._flow_depth_exceeds("[" * (limit + 1), limit) is True
    assert fleet_entries._flow_depth_exceeds("{" * (limit + 1), limit) is True


def test_flow_depth_close_bracket_decrements_by_one() -> None:
    """A close bracket drops exactly one level, not back to the top."""
    # depth 4, close -> 3, then two opens -> 5 > 4
    assert fleet_entries._flow_depth_exceeds("[[[[]" + "[[", 4) is True
    # depth 4, close -> 3, one open -> 4: never deeper than 4
    assert fleet_entries._flow_depth_exceeds("[[[{}" + "[", 4) is False
    # balanced siblings never accumulate
    assert fleet_entries._flow_depth_exceeds("[]" * 50, 1) is False


def test_flow_depth_ignores_stray_close_at_top_level() -> None:
    """[contract] Stray closers at depth 0 do not bank negative depth."""
    assert fleet_entries._flow_depth_exceeds("]]]}" + "[[", 1) is True
    assert fleet_entries._flow_depth_exceeds("", 0) is False
    assert fleet_entries._flow_depth_exceeds("no brackets here", 0) is False


def test_contract_load_rejects_deep_flow_with_named_error() -> None:
    """[contract] Too-deep flow nesting raises RecursionError naming the cap."""
    with pytest.raises(RecursionError, match=r"^flow nesting deeper than 256$"):
        load_fleet_yaml("[" * 257)


# ---------------------------------------------------------------------------
# FleetSafeLoader
# ---------------------------------------------------------------------------


def test_contract_duplicate_keys_are_counted_exactly() -> None:
    """[contract] Every repeated key counts once, and values are all kept."""
    data, duplicates = load_fleet_yaml("a: 1\na: 2\na: 3\nb: 1\nb: 2\nc: 0\n")
    assert duplicates == 3
    assert data == {"a": [1, 2, 3], "b": [1, 2], "c": 0}
    assert load_fleet_yaml("a: 1\na: 2\n") == ({"a": [1, 2]}, 1)


def test_duplicate_personal_keys_merge_to_the_non_false_value() -> None:
    assert load_fleet_yaml("personal: false\npersonal: true\n") == ({"personal": True}, 1)
    assert load_fleet_yaml("personal: yes\npersonal: false\n") == ({"personal": True}, 1)
    assert load_fleet_yaml("personal: false\npersonal: no\n") == ({"personal": False}, 1)


def test_merge_keys_are_not_duplicates() -> None:
    data, duplicates = load_fleet_yaml(
        "base: &b {serial: X, personal: true}\nent:\n  <<: *b\n  personal: false\n"
    )
    assert duplicates == 0
    assert data["ent"] == {"serial": "X", "personal": False}


def test_contract_non_mapping_node_error_names_node_and_mark() -> None:
    """[contract] construct_mapping on a scalar raises with the node's own mark."""
    loader = FleetSafeLoader("scalar\n")
    node = loader.get_single_node()
    with pytest.raises(yaml.constructor.ConstructorError) as info:
        loader.construct_mapping(node)
    assert info.value.context is None
    assert info.value.context_mark is None
    assert info.value.problem == "expected a mapping node, but found scalar"
    assert info.value.problem_mark is node.start_mark


def test_contract_unhashable_key_error_names_key_mark() -> None:
    """[contract] A sequence key raises 'found unhashable key' at the key's mark."""
    loader = FleetSafeLoader("? [1]\n: v\n")
    node = loader.get_single_node()
    with pytest.raises(yaml.constructor.ConstructorError) as info:
        loader.construct_mapping(node)
    assert info.value.context is None
    assert info.value.context_mark is None
    assert info.value.problem == "found unhashable key"
    assert info.value.problem_mark is node.value[0][0].start_mark


def test_deep_construction_is_forwarded_to_values() -> None:
    """deep=True builds nested containers before construct_mapping returns."""
    loader = FleetSafeLoader("k: [1, 2]\nj: {x: [3]}\n")
    node = loader.get_single_node()
    assert loader.construct_mapping(node, deep=True) == {"k": [1, 2], "j": {"x": [3]}}
    shallow = FleetSafeLoader("k: [1, 2]\n")
    assert shallow.construct_mapping(shallow.get_single_node()) == {"k": []}


def test_deep_construction_is_forwarded_to_keys() -> None:
    """deep=True builds a mapping key (and counts its duplicates) before rejecting it."""
    loader = FleetSafeLoader("? {a: 1, a: 2}\n: v\n")
    node = loader.get_single_node()
    with pytest.raises(yaml.constructor.ConstructorError, match="found unhashable key"):
        loader.construct_mapping(node, deep=True)
    assert loader.duplicate_keys == 1


def test_document_load_is_shallow_by_default() -> None:
    """The default (shallow) path rejects a mapping key before building it."""
    loader = FleetSafeLoader("? {a: 1, a: 2}\n: v\n")
    with pytest.raises(yaml.constructor.ConstructorError, match="found unhashable key"):
        loader.get_single_data()
    assert loader.duplicate_keys == 0


def test_contract_numeric_scalars_keep_raw_token_and_value() -> None:
    """[contract] Floats and ints load as numbers that remember their source text."""
    data, duplicates = load_fleet_yaml("serial: 1.50e+3\nid: 0x1F\nhardware_id: 007\n")
    assert duplicates == 0
    assert isinstance(data["serial"], fleet_entries._RawScalarFloat)
    assert data["serial"] == 1500.0
    assert data["serial"].raw_token == "1.50e+3"
    assert isinstance(data["id"], fleet_entries._RawScalarInt)
    assert data["id"] == 31
    assert data["id"].raw_token == "0x1F"
    assert data["hardware_id"] == 7
    assert data["hardware_id"].raw_token == "007"


# ---------------------------------------------------------------------------
# _scalar_to_token / _coerce_identifier_tokens / _extract_personal
# ---------------------------------------------------------------------------


def _loaded(text: str) -> object:
    return load_fleet_yaml(f"v: {text}\n")[0]["v"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ("", False)),
        (True, ("", False)),
        (False, ("", False)),
        ("  FAKE Serial01 ", ("fake serial01", False)),
        ("", ("", False)),
        (b" FAKEBIN01 ", ("fakebin01", True)),
        (bytearray(b"FAKEBIN02"), ("fakebin02", True)),
        (b"\xff\xfe", ("", True)),
        (datetime.date(2024, 1, 2), ("2024-01-02", False)),
        (42, ("42", False)),
    ],
)
def test_contract_scalar_to_token_exact(value: object, expected: tuple[str, bool]) -> None:
    """[contract] Every scalar branch returns its exact ``(token, ambiguous)`` pair."""
    assert fleet_entries._scalar_to_token(value) == expected


def test_scalar_to_token_uses_raw_numeric_token() -> None:
    assert fleet_entries._scalar_to_token(_loaded("0X1f")) == ("0x1f", False)
    assert fleet_entries._scalar_to_token(_loaded("1.50E+3")) == ("1.50e+3", False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ([], False, False)),
        ([], ([], True, False)),
        ("  ", ([], False, False)),
        (" FAKE01 ", (["fake01"], False, False)),
        (["FAKE01", "fake01", "FAKE02"], (["fake01", "fake02"], True, False)),
        ([None, True, ""], ([], True, False)),
        ({"k": "FAKE01"}, (["fake01"], True, True)),
        ([b"FAKE01"], (["fake01"], True, True)),
    ],
)
def test_contract_coerce_identifier_tokens_exact(
    raw: object, expected: tuple[list[str], bool, bool]
) -> None:
    """[contract] ``(tokens, malformed, ambiguous)`` per identifier shape."""
    assert fleet_entries._coerce_identifier_tokens(raw) == expected


def test_coerce_identifier_tokens_skips_a_repeated_container_and_continues() -> None:
    """A container seen twice is skipped, and the walk carries on past it."""
    shared = ["FAKE01"]
    assert fleet_entries._coerce_identifier_tokens([shared, shared, "FAKE02"]) == (
        ["fake01", "fake02"],
        True,
        False,
    )
    cyclic: list[object] = ["FAKE03"]
    cyclic.append(cyclic)
    assert fleet_entries._coerce_identifier_tokens(cyclic) == (["fake03"], True, False)


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({}, (None, False)),
        ({"serial": "X"}, (None, False)),
        ({"personal": False}, (False, False)),
        ({"personal": True}, (True, False)),
        ({"personal": None}, (True, True)),
        ({"personal": 0}, (True, True)),
        ({"personal": "false"}, (True, True)),
        ({"personal": [False]}, (True, True)),
    ],
)
def test_contract_extract_personal_exact(node: dict[str, object], expected: tuple) -> None:
    """[contract] Only a YAML boolean is well formed; anything else is personal + malformed."""
    assert fleet_entries._extract_personal(node) == expected


def test_extract_entry_without_identifier_is_not_ambiguous() -> None:
    assert fleet_entries._extract_entry({"name": "x"}) == (None, False)
    assert fleet_entries._extract_entry({"serial": None}) == (None, False)
    assert fleet_entries._extract_entry({"serial": {"k": None}}) == (None, True)
    assert fleet_entries._extract_entry({"serial": "A", "id": ["B"], "personal": False}) == (
        FleetEntry(("a", "b"), False, True),
        False,
    )


# ---------------------------------------------------------------------------
# _walk_section_nodes: exact counters
# ---------------------------------------------------------------------------


def test_contract_walk_counts_each_cycle_inside_an_entry() -> None:
    """[contract] Two self-references inside one entry are two attributed bad nodes."""
    entry: dict[str, object] = {"serial": "FAKE01"}
    entry["x"] = entry
    entry["y"] = entry
    assert fleet_entries._walk_section_nodes(entry) == ([entry], 0, 2)


def test_contract_walk_counts_every_stray_scalar_across_lists() -> None:
    """[contract] Stray scalars in several lists all add up (3 here, 1 each)."""
    root = [["a"], ["b", "c"]]
    assert fleet_entries._walk_section_nodes(root) == ([], 3, 0)
    assert fleet_entries._walk_section_nodes(["a", "b"]) == ([], 2, 0)


def test_walk_counts_unattributed_cycles_and_visits_shared_nodes_once() -> None:
    shared = {"serial": "FAKE01"}
    other = {"serial": "FAKE02"}
    assert fleet_entries._walk_section_nodes([shared, shared, other]) == ([shared, other], 0, 0)
    loop: list[object] = []
    loop.append(loop)
    assert fleet_entries._walk_section_nodes(loop) == ([], 1, 0)
    loop.append(loop)
    assert fleet_entries._walk_section_nodes(loop) == ([], 2, 0)


def test_contract_cycle_inside_an_entry_is_malformed_but_identified() -> None:
    """[contract] An entry's self-reference is malformed, yet the entry keeps its identity."""
    entry: dict[str, object] = {"serial": "FAKE01", "personal": False}
    entry["self"] = entry
    acc = FleetAccumulator()
    assert collect_section("ios", [entry], acc) is True
    assert _counts(acc) == (1, 0, {"fake01": (FleetEntry(("fake01",), False),)})


# ---------------------------------------------------------------------------
# collect_section / collect_mapping
# ---------------------------------------------------------------------------


def _counts(acc: FleetAccumulator) -> tuple[int, int, dict[str, tuple[FleetEntry, ...]]]:
    return acc.malformed, acc.unidentified, acc.by_id()


def test_contract_null_section_is_readable_and_empty() -> None:
    """[contract] ``ios:`` with no value is an empty, readable section."""
    acc = FleetAccumulator()
    assert collect_section("ios", None, acc) is True
    assert _counts(acc) == (0, 0, {})
    assert collect_mapping({"ios": None}, FleetAccumulator()) is True


def test_contract_repeated_devices_sections_keep_devices_semantics() -> None:
    """[contract] Every copy of a repeated ``devices:`` is read as a devices list."""
    data, duplicates = load_fleet_yaml("devices:\n  - name: a\ndevices:\n  - serial: B\n")
    assert duplicates == 1
    acc = FleetAccumulator()
    assert collect_section("devices", data["devices"], acc) is True
    assert _counts(acc) == (1, 1, {"b": (FleetEntry(("b",), None),)})
    unreadable = FleetAccumulator()
    assert collect_section("ios", fleet_entries._DuplicateValues(["x", "y"]), unreadable) is False
    assert collect_section("ios", fleet_entries._DuplicateValues(["x", None]), unreadable) is True


def test_contract_scalar_section_is_unreadable() -> None:
    """[contract] A scalar platform section is unreadable and one unidentified bad entry."""
    acc = FleetAccumulator()
    assert collect_section("ios", "scalar", acc) is False
    assert _counts(acc) == (1, 1, {})


def test_contract_empty_mapping_section_is_readable() -> None:
    """[contract] A mapping section with no entries and no stray scalars is readable."""
    acc = FleetAccumulator()
    assert collect_section("ios", {"name": "x"}, acc) is True
    assert _counts(acc) == (0, 0, {})


def test_section_with_only_stray_scalars_is_unreadable() -> None:
    acc = FleetAccumulator()
    assert collect_section("garmin", ["a"], acc) is False
    assert _counts(acc) == (1, 1, {})


def test_contract_devices_mapping_without_entries_is_unidentified() -> None:
    """[contract] ``devices:`` as a mapping with no entry is malformed AND unidentified."""
    acc = FleetAccumulator()
    assert collect_section("devices", {"name": "x"}, acc) is True
    assert _counts(acc) == (1, 1, {})


def test_devices_mapping_with_an_entry_is_malformed_but_identified() -> None:
    acc = FleetAccumulator()
    assert collect_section("devices", {"lab": {"serial": "A", "personal": False}}, acc) is True
    assert _counts(acc) == (1, 0, {"a": (FleetEntry(("a",), False),)})


def test_contract_nested_devices_list_still_registers_its_entries() -> None:
    """[contract] ``- - serial: X`` is malformed and unidentified, yet X is registered."""
    acc = FleetAccumulator()
    assert collect_section("devices", [[{"serial": "A", "personal": False}]], acc) is True
    assert _counts(acc) == (1, 1, {"a": (FleetEntry(("a",), False),)})


def test_contract_collect_mapping_passes_each_section_name() -> None:
    """[contract] ``devices:`` keeps its list semantics when read via collect_mapping."""
    acc = FleetAccumulator()
    assert collect_mapping({"devices": [{"name": "x"}], "ios": [{"name": "y"}]}, acc) is True
    assert _counts(acc) == (1, 1, {})


def test_contract_collect_mapping_readable_unless_every_present_section_fails() -> None:
    """[contract] No sections -> readable; all present unreadable -> unreadable."""
    assert collect_mapping({}, FleetAccumulator()) is True
    assert collect_mapping({"other": "x"}, FleetAccumulator()) is True
    assert collect_mapping({"ios": "x", "garmin": "y"}, FleetAccumulator()) is False
    acc = FleetAccumulator()
    assert collect_mapping({"ios": "x", "devices": [{"serial": "A"}]}, acc) is True
    assert _counts(acc) == (1, 1, {"a": (FleetEntry(("a",), None),)})


def test_contract_module_grammar_constants() -> None:
    """[contract] The section names, identifier keys and load errors the policy reads."""
    assert fleet_entries.PLATFORM_SECTIONS == ("devices", "ios", "garmin")
    assert fleet_entries.ENTRY_KEYS == ("serial", "id", "hardware_id")
    assert {yaml.YAMLError, RecursionError, ValueError, TypeError, OverflowError} == set(
        fleet_entries.YAML_LOAD_ERRORS
    )
    assert all(isinstance(c, fleet_entries._CONTAINERS) for c in ({}, [], (), set(), frozenset()))
    assert not isinstance("serial", fleet_entries._CONTAINERS)
    assert fleet_entries._TEXT_ENCODING == "utf-8"
    assert fleet_entries._MAX_FLOW_DEPTH == 256
    assert fleet_entries._FLOW_CHAR_RE.findall("a[b]{c}'[") == ["[", "]", "{", "}", "["]
    assert frozenset("[{") == fleet_entries._FLOW_OPENERS
