"""Whole-file parse of ``device-fleet.yaml`` with budgeted, fail-closed line recovery.

When the document as a whole is not valid YAML, it is split into top-level blocks
and indented chunks that are parsed independently, so one bad line keeps the good
entries. A chunk that still fails is never dropped: its identifiers are scanned
from the raw text and registered as personal unless every ``personal:`` value in
it is a YAML ``false`` (then the flag is unknown). Any registry rebuilt this way is
*degraded*. Recovery is linear (each chunk is parsed once per nesting level, no
re-parse of earlier text) and capped by chunk count, bytes and wall clock; past
the budget the registry is unreadable.
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from linc_nerve.core.fleet_entries import (
    PLATFORM_SECTIONS,
    YAML_LOAD_ERRORS,
    FleetAccumulator,
    FleetEntry,
    collect_mapping,
    collect_section,
    load_fleet_yaml,
)

_MAX_RECOVERY_DEPTH = 6
_MAX_RECOVERY_CHUNKS = 4096
_RECOVERY_BYTE_BUDGET = 2 * 1024 * 1024
_RECOVERY_DEADLINE_S = 2.0

_TOP_KEY_RE = re.compile(
    r'\A(?:"(?P<dq>[^"\r\n]*)"|\'(?P<sq>[^\'\r\n]*)\''
    r"|(?P<bare>[^\s:#\-\[\{\"\'\r\n][^:\r\n]*?))\s*:(?:\s|\Z|#)"
)
_SUB_KEY_RE = re.compile(
    r'^\s+(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s:#\-\[\{\"\'\r\n][^:\r\n]*?)\s*:(?:\s|\Z|#)'
)
_KEY_PREFIX = r"(?:^|(?<=[\s{,\[]))[\"']?"
# ``rest`` is a lookahead so ``finditer`` also finds a second identifier key later on the
# same line (``{id: x, serial: y`` with a missing ``}``): every key must be registered.
_ENTRY_KEY_RE = re.compile(
    _KEY_PREFIX + r"(?:serial|id|hardware_id)[\"']?\s*:(?=\s|$)(?=(?P<rest>.*))"
)
_OWN_ENTRY_KEY_RE = re.compile(r"[\"']?(?:serial|id|hardware_id)[\"']?\s*:(?:\s|$)")
_PERSONAL_RE = re.compile(_KEY_PREFIX + r"personal[\"']?\s*:[ \t]*(?P<value>[^\s,}\]#]*)")
_PROPERTY_RE = re.compile(r"\A(?:(?:&|!)\S*\s+)+")
_PLAIN_END_RE = re.compile(r"\s#|[,}\]]")
_EMPTY_VALUE_CHARS = " \t\r\n,}]"  # a `serial:` followed only by these may be a block list
# Every spelling PyYAML's SafeLoader resolves to boolean ``false`` (YAML 1.1).
_YAML_FALSE = frozenset({"false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"})
_NULL_TOKENS = frozenset({"", "null", "~"})
_FAILED = object()
_TEXT_ENCODING = "utf-8"


@dataclass(frozen=True)
class FleetParse:
    """Result of parsing a fleet document's text."""

    readable: bool
    by_id: Mapping[str, tuple[FleetEntry, ...]]
    error: str | None = None
    malformed_count: int = 0
    degraded: bool = False


class _BudgetExceeded(Exception):
    pass


def _recovery_clock() -> float:
    """Monotonic clock for the recovery deadline (patched in tests)."""
    return time.monotonic()


class _Budget:
    def __init__(self) -> None:
        self.chunks = 0
        self.nbytes = 0
        self.deadline = _recovery_clock() + _RECOVERY_DEADLINE_S

    def charge(self, text: str) -> None:
        self.chunks += 1
        self.nbytes += len(text)
        if self.chunks > _MAX_RECOVERY_CHUNKS:
            raise _BudgetExceeded(f"more than {_MAX_RECOVERY_CHUNKS} chunks")
        if self.nbytes > _RECOVERY_BYTE_BUDGET:
            raise _BudgetExceeded(f"more than {_RECOVERY_BYTE_BUDGET} bytes parsed")
        if _recovery_clock() > self.deadline:
            raise _BudgetExceeded(f"longer than {_RECOVERY_DEADLINE_S}s")


def parse_fleet_text(text: str) -> FleetParse:
    """Parse fleet *text*; never raises on hostile content."""
    try:
        data, duplicates = load_fleet_yaml(text)
    except YAML_LOAD_ERRORS as exc:
        return _recover(text, exc)
    if not isinstance(data, Mapping):
        return FleetParse(False, {}, "root YAML value must be a mapping")
    acc = FleetAccumulator(malformed=duplicates)
    if not collect_mapping(data, acc):
        return FleetParse(
            False,
            {},
            "no readable platform section in device fleet registry",
            malformed_count=acc.malformed,
        )
    error = None
    if acc.unidentified:
        error = f"{acc.unidentified} malformed fleet entries carry no usable identifier"
    return FleetParse(True, acc.by_id(), error, acc.malformed, degraded=bool(acc.unidentified))


def _recover(text: str, exc: BaseException) -> FleetParse:
    acc = FleetAccumulator()
    budget = _Budget()
    sections_ok: list[bool] = []
    try:
        for name, lines in _split_top_level_blocks(text):
            if name in PLATFORM_SECTIONS:
                ok = _recover_chunk(name, lines, acc, budget, depth=0, parse_first=True)
                sections_ok.append(ok)
            else:
                _recover_other_block(name, lines, acc, budget)
    except _BudgetExceeded as over:
        return FleetParse(
            False,
            {},
            f"device fleet registry recovery budget exceeded ({over}) after {type(exc).__name__}",
            malformed_count=max(acc.malformed, 1),
        )
    malformed = max(acc.malformed, 1)  # the document itself failed to parse
    if not any(sections_ok) and not acc.grouped:
        return FleetParse(False, {}, f"{type(exc).__name__}: {exc}", malformed_count=malformed)
    error = f"recovered after {type(exc).__name__} with {malformed} malformed entries"
    return FleetParse(True, acc.by_id(), error, malformed, degraded=True)


def _try_parse(text: str, acc: FleetAccumulator, budget: _Budget) -> tuple[Any, bool]:
    """Return ``(data or _FAILED, failed_on_recursion)`` for one budgeted parse."""
    budget.charge(text)
    try:
        data, duplicates = load_fleet_yaml(text)
    except RecursionError:
        return _FAILED, True
    except YAML_LOAD_ERRORS:
        return _FAILED, False
    acc.malformed += duplicates
    return data, False


def _recover_other_block(
    name: str | None, lines: Sequence[str], acc: FleetAccumulator, budget: _Budget
) -> None:
    """A non-platform block only matters if it is broken and names an identifier."""
    if not any(_ENTRY_KEY_RE.search(line) for line in lines):
        if name is None:
            acc.bad()
        return
    if name is not None and _try_parse("".join(lines), acc, budget)[0] is not _FAILED:
        return
    _register_leaf(lines, acc)


def _recover_chunk(
    section: str,
    lines: Sequence[str],
    acc: FleetAccumulator,
    budget: _Budget,
    *,
    depth: int,
    parse_first: bool,
) -> bool:
    """Parse one chunk of *section*, descending into its body when it fails.

    A chunk that failed on nesting depth and has a single sub-chunk is descended
    without re-parsing it (``parse_first=False``: peeling one level never cures a
    deep nest), so a deep single chain costs one parse, not one per level.
    """
    loaded, too_deep = _try_parse("".join(lines), acc, budget) if parse_first else (_FAILED, True)
    if loaded is not _FAILED:
        value = loaded.get(section) if depth == 0 and isinstance(loaded, Mapping) else loaded
        return collect_section(section, value, acc)
    subchunks = _split_indented_chunks(lines[1:])
    if depth >= _MAX_RECOVERY_DEPTH or not subchunks or _is_entry_chunk(lines):
        _register_leaf(lines, acc)
        return False
    chain = too_deep and len(subchunks) == 1
    results = [
        _recover_chunk(section, sub, acc, budget, depth=depth + 1, parse_first=not chain)
        for sub in subchunks
    ]
    return any(results)


def _content_lines(lines: Sequence[str]) -> list[str]:
    return [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _is_entry_chunk(lines: Sequence[str]) -> bool:
    """True when the chunk is one device entry (its own keys include an identifier)."""
    if _ENTRY_KEY_RE.search(lines[0]):
        return True
    body = _content_lines(lines[1:])
    if not body:
        return False
    base = min(_line_indent(ln) for ln in body)
    return any(
        _line_indent(ln) == base and _OWN_ENTRY_KEY_RE.match(ln.lstrip()) is not None
        for ln in body
    )


def _register_leaf(lines: Sequence[str], acc: FleetAccumulator) -> None:
    """Register the identifiers of a chunk that cannot be parsed (fail closed)."""
    tokens = _scan_identifiers(lines)
    if not tokens:
        acc.bad()
        return
    values = [m.group("value") for ln in lines for m in _PERSONAL_RE.finditer(ln)]
    cleared = bool(values) and all(v in _YAML_FALSE for v in values)
    acc.add(FleetEntry(tuple(tokens), None if cleared else True, malformed=True), ambiguous=True)


def _scan_identifiers(lines: Sequence[str]) -> list[str]:
    tokens: list[str] = []
    for index, line in enumerate(lines):
        for match in _ENTRY_KEY_RE.finditer(line):
            found = _value_tokens(match.group("rest"))
            if not found and not match.group("rest").strip(_EMPTY_VALUE_CHARS):
                found = _block_list_tokens(lines, index)
            tokens.extend(tok for tok in found if tok not in tokens)
    return tokens


def _block_list_tokens(lines: Sequence[str], index: int) -> list[str]:
    """Tokens of a block sequence (``- X`` lines) following an empty ``serial:``."""
    key_indent = _line_indent(lines[index])
    found: list[str] = []
    for line in lines[index + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _line_indent(line) < key_indent or not stripped.startswith("- "):
            break
        found.extend(_value_tokens(stripped[2:]))
    return found


def _value_tokens(rest: str) -> list[str]:
    """Best-effort identifier tokens from the raw text after ``serial:``."""
    value = rest.strip()
    binary = value.startswith("!!binary")
    value = _PROPERTY_RE.sub("", value)
    if not value or value[0] in "#*|>":
        return []
    if value[0] == "[":
        parts = value[1:].partition("]")[0].split(",")
    elif value[0] == "{":
        parts = [seg.split(":", 1)[-1] for seg in value[1:].partition("}")[0].split(",")]
    elif value[0] in "\"'":
        parts = [value[1:].partition(value[0])[0]]  # up to the closing quote, if any
    else:
        stop = _PLAIN_END_RE.search(value)
        parts = [value[: stop.start()] if stop else value]
    tokens = [_clean_token(part, binary=binary) for part in parts]
    return [tok for tok in tokens if tok not in _NULL_TOKENS]


def _clean_token(part: str, *, binary: bool) -> str:
    token = _PROPERTY_RE.sub("", part.strip()).strip().strip("\"'").strip()
    if binary:
        try:
            token = base64.b64decode(token).decode(_TEXT_ENCODING)
        except (binascii.Error, ValueError):
            return ""
    return token.strip().casefold()


def _split_top_level_blocks(text: str) -> list[tuple[str | None, list[str]]]:
    """Split raw YAML text into top-level ``(section_key, lines)`` blocks."""
    blocks: list[tuple[str | None, list[str]]] = []
    current: tuple[str | None, list[str]] | None = None
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
            if current is not None:
                current[1].append(line)
            continue
        if line[0].isspace():
            if current is None:
                current = (None, [line])
            else:
                current[1].append(line)
            continue
        if current is not None:
            blocks.append(current)
        match = _TOP_KEY_RE.match(line)
        if match is not None:
            current = (next(g for g in match.groups() if g is not None), [line])
        else:
            blocks.append((None, [line]))
            current = None
    if current is not None:
        blocks.append(current)
    return blocks


def _split_indented_chunks(lines: Sequence[str]) -> list[list[str]]:
    """Split indented YAML body lines into entry/sub-section chunks."""
    content = _content_lines(lines)
    if not content:
        return []
    base = min(_line_indent(ln) for ln in content)
    chunks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if current:
                current.append(line)
            continue
        if _line_indent(line) > base:
            current.append(line)
            continue
        if current:
            chunks.append(current)
            current = []
        if stripped.startswith("-") or _SUB_KEY_RE.match(line):
            current = [line]
        else:
            chunks.append([line])
    if current:
        chunks.append(current)
    return chunks
