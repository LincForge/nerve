"""Exact-value unit tests for ``core/fleet_parse`` (bead linc-nerve-56eg).

Whole ``FleetParse`` results are compared by equality, so a mutated message,
count, flag or identifier map fails a test instead of passing a looser check.
"""

from __future__ import annotations

import re

import pytest
import yaml

import linc_nerve.core.ios  # noqa: F401  (conftest imports ios_discovery, which needs ios first)
from linc_nerve.core import fleet_parse
from linc_nerve.core.fleet_entries import FleetAccumulator, FleetEntry
from linc_nerve.core.fleet_parse import FleetParse, parse_fleet_text

LAB = "FAKELAB01"
_LAB_ENTRY = {"fakelab01": (FleetEntry(("fakelab01",), False),)}
_LAB_BLOCK = f"devices:\n  - serial: {LAB}\n    personal: false\n"


def _bad_entries(n: int) -> str:
    return "".join(
        f"  - serial: FAKEBAD{i:02d}\n    notes: a: b\n    personal: true\n" for i in range(n)
    )


def _scanned(*serials: str) -> dict[str, tuple[FleetEntry, ...]]:
    return {s: (FleetEntry((s,), True, malformed=True),) for s in serials}


# ---------------------------------------------------------------------------
# parse_fleet_text: whole-result equality per branch
# ---------------------------------------------------------------------------


def test_contract_clean_registry_parses_exactly() -> None:
    """[contract] A valid registry is readable, undegraded and error-free."""
    assert parse_fleet_text(_LAB_BLOCK) == FleetParse(True, _LAB_ENTRY)


def test_contract_non_mapping_root_is_unreadable_with_message() -> None:
    """[contract] A list root is unreadable with the exact reason."""
    assert parse_fleet_text("- a\n") == FleetParse(False, {}, "root YAML value must be a mapping")


def test_contract_no_readable_section_is_unreadable_with_count() -> None:
    """[contract] Every present section unreadable -> unreadable, count preserved."""
    assert parse_fleet_text("ios: scalar\n") == FleetParse(
        False, {}, "no readable platform section in device fleet registry", malformed_count=1
    )


def test_contract_unidentified_entries_degrade_with_message() -> None:
    """[contract] Entries with no usable identifier degrade a parsed registry."""
    text = _LAB_BLOCK + "  - name: a\n  - serial: {k: FAKE01}\n"
    assert parse_fleet_text(text) == FleetParse(
        True,
        {**_LAB_ENTRY, "fake01": (FleetEntry(("fake01",), None, malformed=True),)},
        "2 malformed fleet entries carry no usable identifier",
        2,
        degraded=True,
    )


def test_contract_duplicate_keys_count_as_malformed() -> None:
    text = _LAB_BLOCK + "ios:\n  - serial: FAKEIOS01\n    personal: false\n    personal: false\n"
    result = parse_fleet_text(text)
    assert result.malformed_count == 1
    assert result.degraded is False
    assert result.error is None


# ---------------------------------------------------------------------------
# _recover: exact results
# ---------------------------------------------------------------------------


def test_contract_recovery_counts_the_failed_document_once() -> None:
    """[contract] A broken non-platform block costs exactly one malformed entry."""
    assert parse_fleet_text(_LAB_BLOCK + "garbage: [unclosed\n") == FleetParse(
        True, _LAB_ENTRY, "recovered after ParserError with 1 malformed entries", 1, True
    )


def test_contract_unrecoverable_document_is_unreadable_with_parser_error() -> None:
    """[contract] Nothing recovered -> unreadable, naming the original exception."""
    result = parse_fleet_text("devices: [\n")
    assert result.readable is False
    assert result.by_id == {}
    assert result.error is not None
    assert result.error.startswith("ParserError: while parsing a flow node\n")
    assert result.malformed_count == 1
    assert result.degraded is False


def test_contract_recovery_budget_exceeded_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """[contract] Past the chunk cap: unreadable, empty, exact reason and count."""
    monkeypatch.setattr(fleet_parse, "_MAX_RECOVERY_CHUNKS", 2)
    assert parse_fleet_text("devices:\n" + _bad_entries(4)) == FleetParse(
        False,
        {},
        "device fleet registry recovery budget exceeded (more than 2 chunks) after ScannerError",
        malformed_count=1,
    )


def test_recovery_within_budget_scans_every_bad_entry() -> None:
    assert parse_fleet_text("devices:\n" + _bad_entries(2)) == FleetParse(
        True,
        _scanned("fakebad00", "fakebad01"),
        "recovered after ScannerError with 2 malformed entries",
        2,
        True,
    )


def test_contract_recovered_devices_section_keeps_devices_semantics() -> None:
    """[contract] A recovered ``devices:`` list still counts id-less items as malformed."""
    text = _LAB_BLOCK + "  - name: a\n  - name: b\ngarbage: [unclosed\n"
    assert parse_fleet_text(text) == FleetParse(
        True, _LAB_ENTRY, "recovered after ParserError with 2 malformed entries", 2, True
    )


def test_contract_recovered_devices_subchunks_keep_devices_semantics() -> None:
    """[contract] Sub-chunks of a failed ``devices:`` block are still devices entries."""
    text = _LAB_BLOCK + "  - name: a\n  - name: b\n  - serial: [broken\n"
    assert parse_fleet_text(text) == FleetParse(
        True,
        {**_LAB_ENTRY, **_scanned("broken")},
        "recovered after ParserError with 3 malformed entries",
        3,
        True,
    )


# ---------------------------------------------------------------------------
# _Budget / _try_parse / _recover_chunk
# ---------------------------------------------------------------------------


def test_contract_budget_starts_empty_and_byte_cap_is_inclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] Exactly the byte budget passes; one more byte trips it."""
    monkeypatch.setattr(fleet_parse, "_RECOVERY_BYTE_BUDGET", 10)
    budget = fleet_parse._Budget()
    assert (budget.chunks, budget.nbytes) == (0, 0)
    budget.charge("x" * 10)
    assert (budget.chunks, budget.nbytes) == (1, 10)
    with pytest.raises(fleet_parse._BudgetExceeded, match=r"^more than 10 bytes parsed$"):
        budget.charge("x")


def test_contract_budget_chunk_cap_is_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    """[contract] Exactly the chunk cap passes; the next chunk trips it."""
    monkeypatch.setattr(fleet_parse, "_MAX_RECOVERY_CHUNKS", 2)
    budget = fleet_parse._Budget()
    budget.charge("")
    budget.charge("")
    assert budget.chunks == 2
    with pytest.raises(fleet_parse._BudgetExceeded, match=r"^more than 2 chunks$"):
        budget.charge("")


def test_contract_budget_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """[contract] The deadline is start + _RECOVERY_DEADLINE_S, checked per charge."""
    ticks = iter([100.0, 102.0, 102.5])
    monkeypatch.setattr(fleet_parse, "_recovery_clock", lambda: next(ticks))
    budget = fleet_parse._Budget()
    assert budget.deadline == 102.0
    budget.charge("a")
    with pytest.raises(fleet_parse._BudgetExceeded, match=r"^longer than 2.0s$"):
        budget.charge("a")


def test_contract_try_parse_reports_recursion_separately() -> None:
    """[contract] Only a RecursionError reports ``too_deep``; duplicates are counted."""
    acc = FleetAccumulator()
    budget = fleet_parse._Budget()
    data, too_deep = fleet_parse._try_parse("a: [\n", acc, budget)
    assert data is fleet_parse._FAILED and too_deep is False
    data, too_deep = fleet_parse._try_parse("[" * 300, acc, budget)
    assert data is fleet_parse._FAILED and too_deep is True
    assert fleet_parse._try_parse("a: 1\nb: 1\nb: 2\n", acc, budget) == (
        {"a": 1, "b": [1, 2]},
        False,
    )
    assert acc.malformed == 1
    assert budget.chunks == 3


_DEEP_LINES = [
    "  group:\n",
    "    - serial: FAKE1\n",
    "      personal: false\n",
    "    - [broken\n",
]


def test_contract_recovery_depth_cap_registers_a_leaf() -> None:
    """[contract] At the depth cap the chunk is scanned as one fail-closed leaf."""
    acc = FleetAccumulator()
    assert (
        fleet_parse._recover_chunk(
            "ios", _DEEP_LINES, acc, fleet_parse._Budget(), depth=6, parse_first=True
        )
        is False
    )
    assert acc.by_id() == {"fake1": (FleetEntry(("fake1",), None, malformed=True),)}
    assert (acc.malformed, acc.unidentified) == (1, 1)


def test_recovery_below_depth_cap_descends_into_sub_chunks() -> None:
    acc = FleetAccumulator()
    assert (
        fleet_parse._recover_chunk(
            "ios", _DEEP_LINES, acc, fleet_parse._Budget(), depth=5, parse_first=True
        )
        is True
    )
    assert acc.by_id() == {"fake1": (FleetEntry(("fake1",), False),)}
    assert (acc.malformed, acc.unidentified) == (1, 1)


def test_recover_chunk_parses_the_chunk_first_when_asked() -> None:
    """The chunk itself is parsed first unless it already failed on depth."""
    lines = ["devices:\n", f"  - serial: {LAB}\n", "    personal: false\n"]
    acc = FleetAccumulator()
    budget = fleet_parse._Budget()
    assert fleet_parse._recover_chunk("devices", lines, acc, budget, depth=0, parse_first=True)
    assert acc.by_id() == _LAB_ENTRY
    assert (acc.malformed, acc.unidentified, budget.chunks) == (0, 0, 1)


def test_contract_deep_chain_is_parsed_once() -> None:
    """[contract] A chain that failed on depth is descended without re-parsing each level."""
    lines = ["ios:\n", "  a:\n", "    b:\n", "      - serial: " + "[" * 300 + "\n"]
    acc = FleetAccumulator()
    budget = fleet_parse._Budget()
    assert (
        fleet_parse._recover_chunk("ios", lines, acc, budget, depth=0, parse_first=True) is False
    )
    assert budget.chunks == 1
    assert (acc.malformed, acc.unidentified) == (1, 1)


def test_contract_single_sub_chunk_after_a_syntax_error_is_parsed() -> None:
    """[contract] Only a depth failure skips re-parsing; a syntax error does not."""
    text = f"ios: [\n  - serial: {LAB}\n    personal: false\n"
    assert parse_fleet_text(text) == FleetParse(
        True, _LAB_ENTRY, "recovered after ParserError with 1 malformed entries", 1, True
    )


def test_contract_recovery_depth_advances_one_level_at_a_time() -> None:
    """[contract] From depth 4 a two-level nest still reaches its parseable entry."""
    lines = [
        "  g:\n",
        "    h:\n",
        "      - serial: FAKE1\n",
        "        personal: false\n",
        "      - [broken\n",
    ]
    acc = FleetAccumulator()
    budget = fleet_parse._Budget()
    assert fleet_parse._recover_chunk("ios", lines, acc, budget, depth=4, parse_first=True)
    assert acc.by_id() == {"fake1": (FleetEntry(("fake1",), False),)}
    capped = FleetAccumulator()  # from depth 5 the inner chunk hits the cap: one leaf
    assert not fleet_parse._recover_chunk("ios", lines, capped, budget, depth=5, parse_first=True)
    assert capped.by_id() == {"fake1": (FleetEntry(("fake1",), None, malformed=True),)}
    assert fleet_parse._MAX_RECOVERY_DEPTH == 6


def test_contract_any_readable_section_keeps_a_recovered_registry_readable() -> None:
    """[contract] An earlier readable section is not forgotten by a later broken one."""
    text = "ios:\n  - name: a\ngarmin: [broken\n"
    result = parse_fleet_text(text)
    assert result.readable is True
    assert result.by_id == {}
    assert result.degraded is True


def test_contract_valid_non_platform_block_is_parsed_whole() -> None:
    """[contract] A named non-platform block that parses is ignored, not scanned."""
    lines = ["notes:\n", "  - [serial: FAKE1,\n", "     b]\n"]
    acc = FleetAccumulator()
    budget = fleet_parse._Budget()
    fleet_parse._recover_other_block("notes", lines, acc, budget)
    assert (acc.by_id(), acc.malformed, acc.unidentified, budget.chunks) == ({}, 0, 0, 1)
    broken = FleetAccumulator()
    fleet_parse._recover_other_block("notes", ["notes: [serial: FAKE1\n"], broken, budget)
    assert broken.by_id() == {"fake1": (FleetEntry(("fake1",), True, malformed=True),)}


def test_split_indented_chunks_exact() -> None:
    lines = ["  - serial: A\n", "  # c\n", "    personal: false\n", "  - serial: B\n"]
    assert fleet_parse._split_indented_chunks(lines) == [
        ["  - serial: A\n", "  # c\n", "    personal: false\n"],
        ["  - serial: B\n"],
    ]
    lines = ["  - serial: A\n", "  plain\n", "    deeper\n", "\n"]
    assert fleet_parse._split_indented_chunks(lines) == [
        ["  - serial: A\n"],
        ["  plain\n"],
        ["    deeper\n", "\n"],
    ]
    assert fleet_parse._split_indented_chunks(["\n", "  # only\n"]) == []


# ---------------------------------------------------------------------------
# Raw-text helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rest", "expected"),
    [
        ("A", ["a"]),
        ("x#y", ["x#y"]),
        ("FAKE#1", ["fake#1"]),
        ("FAKE1 # c", ["fake1"]),
        ("FAKE1, x", ["fake1"]),
        ("*alias", []),
        ("|", []),
        ("> x", []),
        ("# c", []),
        ("", []),
        ("null", []),
        ("~", []),
        ("[A, B]", ["a", "b"]),
        ("[A] B]", ["a"]),
        ("[null, A]", ["a"]),
        ("{k:FAKE1}", ["fake1"]),
        ("{k: a:b}", ["a:b"]),
        ("{a: X, b: Y}", ["x", "y"]),
        ("{a: X}, {b: Y}", ["x"]),
        ("{FAKE1}", ["fake1"]),
        ("['FAKE1', \"B\"]", ["fake1", "b"]),
        ("[&a FAKE1, !t FAKE2]", ["fake1", "fake2"]),
        ('"FAKE1" # "x"', ["fake1"]),
        ('""FAKE"', []),
        ("A'B", ["a'b"]),
        ("XFAKEX", ["xfakex"]),
        ("'FAKE 1' # c", ["fake 1"]),
        ('"FAKE1" # note', ["fake1"]),
        ('"FAKE12', ["fake12"]),
        ("&a FAKE1", ["fake1"]),
        ("!!binary RkFLRUJJTjAx", ["fakebin01"]),
        ("!!binary ***", []),
        ("!!binary //79", []),
    ],
)
def test_contract_value_tokens_exact(rest: str, expected: list[str]) -> None:
    """[contract] Raw ``serial:`` text maps to exactly these tokens."""
    assert fleet_parse._value_tokens(rest) == expected


def test_content_lines_drops_blank_and_comment_lines() -> None:
    lines = ["  # c\n", "\n", "  a: 1\n", "#x\n", "b\n"]
    assert fleet_parse._content_lines(lines) == ["  a: 1\n", "b\n"]


def test_is_entry_chunk_exact() -> None:
    assert fleet_parse._is_entry_chunk(["  - name: x\n"]) is False
    assert fleet_parse._is_entry_chunk(["  - serial: x\n"]) is True
    assert fleet_parse._is_entry_chunk(["  dev:\n", "    serial: A\n"]) is True
    assert fleet_parse._is_entry_chunk(["  dev:\n", "    sub:\n", "      serial: A\n"]) is False


def test_contract_register_leaf_is_personal_ambiguous_and_malformed() -> None:
    """[contract] A scanned leaf is personal, malformed and counted unidentified."""
    acc = FleetAccumulator()
    fleet_parse._register_leaf(["  - serial: FAKE1\n"], acc)
    assert acc.by_id() == {"fake1": (FleetEntry(("fake1",), True, malformed=True),)}
    assert (acc.malformed, acc.unidentified) == (1, 1)
    cleared = FleetAccumulator()
    fleet_parse._register_leaf(["  - serial: FAKE1\n", "    personal: off\n"], cleared)
    assert cleared.by_id() == {"fake1": (FleetEntry(("fake1",), None, malformed=True),)}
    empty = FleetAccumulator()
    fleet_parse._register_leaf(["  - name: x\n"], empty)
    assert (empty.by_id(), empty.malformed, empty.unidentified) == ({}, 1, 1)


def test_scan_identifiers_reads_block_lists_only_after_an_empty_key() -> None:
    assert fleet_parse._scan_identifiers(["serial: ~\n", "  - FAKE2\n"]) == []
    assert fleet_parse._scan_identifiers(["  - {serial: }\n", "    - FAKE3\n"]) == ["fake3"]
    assert fleet_parse._scan_identifiers(["serial:\n", "  - FAKE4\n", "id: FAKE5\n"]) == [
        "fake4",
        "fake5",
    ]


def test_block_list_tokens_accept_items_at_the_key_indent() -> None:
    lines = ["serial:\n", "- FAKE4\n", "\n", "# c\n", "- FAKE5\n", "other: x\n", "- FAKE6\n"]
    assert fleet_parse._block_list_tokens(lines, 0) == ["fake4", "fake5"]
    assert fleet_parse._block_list_tokens(["  serial:\n", "- FAKE4\n"], 0) == []


def test_contract_split_top_level_blocks_exact() -> None:
    """[contract] Comments and markers stay in their block; any column-0 key opens one."""
    text = "devices:\n# note\n  - serial: A\n...\n---\nx y: 1\nb\n  - serial: C\n"
    assert fleet_parse._split_top_level_blocks(text) == [
        ("devices", ["devices:\n", "# note\n", "  - serial: A\n", "...\n", "---\n"]),
        ("x y", ["x y: 1\n"]),
        (None, ["b\n"]),
        (None, ["  - serial: C\n"]),
    ]


def test_split_top_level_blocks_leading_indented_and_comment_lines() -> None:
    assert fleet_parse._split_top_level_blocks("# c\n  - serial: A\nios:\n") == [
        (None, ["  - serial: A\n"]),
        ("ios", ["ios:\n"]),
    ]
    assert fleet_parse._split_top_level_blocks("") == []


def test_split_top_level_blocks_tab_indent_is_continuation() -> None:
    assert fleet_parse._split_top_level_blocks("ios:\n\tx\n") == [("ios", ["ios:\n", "\tx\n"])]


# ---------------------------------------------------------------------------
# Module-level grammar: the regexes and token sets the raw scan relies on
# ---------------------------------------------------------------------------


def test_contract_top_and_sub_key_patterns() -> None:
    """[contract] Top-level keys may be bare or quoted; list items are not keys."""
    top = fleet_parse._TOP_KEY_RE
    assert top.match("devices:\n").group("bare") == "devices"
    assert top.match('"ios": x\n').group("dq") == "ios"
    assert top.match("'garmin':\n").group("sq") == "garmin"
    assert top.match("- serial: x\n") is None
    assert top.match("url: http://x\n").group("bare") == "url"
    assert top.match("a:b\n") is None
    sub = fleet_parse._SUB_KEY_RE
    assert sub.match("  serial: x\n") is not None
    assert sub.match("  'k': x\n") is not None
    assert sub.match("  - serial: x\n") is None
    assert sub.match("serial: x\n") is None


def test_contract_entry_key_patterns() -> None:
    """[contract] Identifier keys are found at a key position only, quoted or not."""
    prefix = re.compile(fleet_parse._KEY_PREFIX + "id")
    assert prefix.search("'id") is not None
    assert prefix.search("{id") is not None
    assert prefix.search("xid") is None
    entry = fleet_parse._ENTRY_KEY_RE
    assert entry.search("  - serial: FAKE1").group("rest") == " FAKE1"
    assert entry.search('{"id": FAKE2}').group("rest") == " FAKE2}"
    assert entry.search("  hardware_id:").group("rest") == ""
    assert entry.search("  model_id: x") is None
    assert entry.search("  serial:x") is None
    own = fleet_parse._OWN_ENTRY_KEY_RE
    assert own.match("serial: x") is not None
    assert own.match("'hardware_id':") is not None
    assert own.match("serials: x") is None


def test_contract_personal_property_and_plain_end_patterns() -> None:
    """[contract] ``personal:`` values, YAML node properties and plain-scalar ends."""
    personal = fleet_parse._PERSONAL_RE
    assert [m.group("value") for m in personal.finditer("{personal: no, 'personal': x}")] == [
        "no",
        "x",
    ]
    assert personal.search("  personal:\n").group("value") == ""
    assert personal.search("impersonal: no") is None
    assert fleet_parse._PROPERTY_RE.sub("", "&a !!str FAKE1") == "FAKE1"
    assert fleet_parse._PROPERTY_RE.sub("", "FAKE1 &a") == "FAKE1 &a"
    assert fleet_parse._PLAIN_END_RE.search("A # c").start() == 1
    assert fleet_parse._PLAIN_END_RE.search("A#c") is None
    assert [fleet_parse._PLAIN_END_RE.search(s).start() for s in ("A,", "A}", "A]")] == [1] * 3
    assert fleet_parse._EMPTY_VALUE_CHARS == " \t\r\n,}]"


def test_contract_yaml_false_and_null_token_sets() -> None:
    """[contract] Exactly PyYAML's boolean-false spellings, and the null tokens."""
    assert {
        "false",
        "False",
        "FALSE",
        "no",
        "No",
        "NO",
        "off",
        "Off",
        "OFF",
    } == fleet_parse._YAML_FALSE
    assert all(yaml.safe_load(f"v: {s}")["v"] is False for s in fleet_parse._YAML_FALSE)
    assert {"", "null", "~"} == fleet_parse._NULL_TOKENS
    assert fleet_parse._TEXT_ENCODING == "utf-8"
