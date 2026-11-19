"""Structural parse of ``device-fleet.yaml``: loader, identifier coercion, section walk.

Everything here is a trust boundary (an operator-edited file), so nothing raises on
hostile content: bad entries are counted, never fatal, and an entry whose identity
cannot be read with confidence is counted as *unidentified* so the registry is
reported degraded (see ``fleet_parse``).
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml

PLATFORM_SECTIONS = ("devices", "ios", "garmin")
ENTRY_KEYS = ("serial", "id", "hardware_id")
YAML_LOAD_ERRORS = (yaml.YAMLError, RecursionError, ValueError, TypeError, OverflowError)
_CONTAINERS = (Mapping, list, tuple, set, frozenset)
_TEXT_ENCODING = "utf-8"


@dataclass(frozen=True)
class FleetEntry:
    """One registered device mapping in ``device-fleet.yaml``."""

    identifiers: tuple[str, ...]
    personal: bool | None
    malformed: bool = False


@dataclass
class FleetAccumulator:
    """Identifier index plus malformed/unidentified counters for one parse."""

    grouped: dict[str, list[FleetEntry]] = field(default_factory=dict)
    malformed: int = 0
    unidentified: int = 0

    def add(self, entry: FleetEntry, *, ambiguous: bool = False) -> None:
        for ident in entry.identifiers:
            self.grouped.setdefault(ident, []).append(entry)
        if entry.malformed:
            self.malformed += 1
        if ambiguous:
            self.unidentified += 1

    def bad(self, *, identified: bool = False) -> None:
        self.malformed += 1
        if not identified:
            self.unidentified += 1

    def by_id(self) -> dict[str, tuple[FleetEntry, ...]]:
        return {ident: tuple(entries) for ident, entries in self.grouped.items()}


class _RawScalarInt(int):
    """Integer loaded from YAML retaining its original scalar token."""

    raw_token: str

    def __new__(cls, value: int, raw_token: str) -> _RawScalarInt:
        obj = super().__new__(cls, value)
        obj.raw_token = raw_token
        return obj


class _RawScalarFloat(float):
    """Float loaded from YAML retaining its original scalar token."""

    raw_token: str

    def __new__(cls, value: float, raw_token: str) -> _RawScalarFloat:
        obj = super().__new__(cls, value)
        obj.raw_token = raw_token
        return obj


class _DuplicateValues(list):  # type: ignore[type-arg]
    """Values of one key repeated in a mapping, kept by reference (merged later)."""


def _combine_duplicate(key: Any, old: Any, new: Any) -> Any:
    """Merge a repeated key: any non-false ``personal`` wins, everything else is kept."""
    if key == "personal":
        return new if old is False else old
    if isinstance(old, _DuplicateValues):
        old.append(new)
        return old
    return _DuplicateValues([old, new])


class FleetSafeLoader(yaml.SafeLoader):
    """SafeLoader keeping raw numeric tokens and merging duplicate mapping keys."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.duplicate_keys = 0

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None, None, f"expected a mapping node, but found {node.id}", node.start_mark
            )
        own = sum(1 for key_node, _ in node.value if key_node.tag != "tag:yaml.org,2002:merge")
        self.flatten_mapping(node)
        merged = len(node.value) - own  # merge-key pairs come first; own keys override them
        mapping: dict[Any, Any] = {}
        own_keys: set[Any] = set()
        for index, (key_node, value_node) in enumerate(node.value):
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, Hashable):
                raise yaml.constructor.ConstructorError(
                    None, None, "found unhashable key", key_node.start_mark
                )
            value = self.construct_object(value_node, deep=deep)
            if index < merged:
                mapping[key] = value
                continue
            if key in own_keys:
                self.duplicate_keys += 1
                mapping[key] = _combine_duplicate(key, mapping[key], value)
                continue
            own_keys.add(key)
            mapping[key] = value
        return mapping


def _construct_fleet_int(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> int:
    return _RawScalarInt(yaml.SafeLoader.construct_yaml_int(loader, node), str(node.value))


def _construct_fleet_float(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> float:
    return _RawScalarFloat(yaml.SafeLoader.construct_yaml_float(loader, node), str(node.value))


FleetSafeLoader.add_constructor("tag:yaml.org,2002:int", _construct_fleet_int)
FleetSafeLoader.add_constructor("tag:yaml.org,2002:float", _construct_fleet_float)


_MAX_FLOW_DEPTH = 256
_FLOW_CHAR_RE = re.compile(r"[\[\]{}]")
_FLOW_OPENERS = frozenset("[{")


def _flow_depth_exceeds(text: str, limit: int) -> bool:
    """True when ``[``/``{`` nest deeper than *limit* (quotes ignored: fail closed)."""
    depth = 0
    for match in _FLOW_CHAR_RE.finditer(text):
        if match.group() in _FLOW_OPENERS:
            depth += 1
            if depth > limit:
                return True
        elif depth:
            depth -= 1
    return False


def load_fleet_yaml(text: str) -> tuple[Any, int]:
    """Parse *text* with ``FleetSafeLoader``; return ``(data, duplicate_key_count)``.

    Flow nesting past ``_MAX_FLOW_DEPTH`` raises ``RecursionError`` up front: PyYAML's
    scanner is quadratic in flow depth, so a deep ``[[[`` costs ~0.5 s per attempt.
    """
    if _flow_depth_exceeds(text, _MAX_FLOW_DEPTH):
        raise RecursionError(f"flow nesting deeper than {_MAX_FLOW_DEPTH}")
    loader = FleetSafeLoader(text)
    try:
        return loader.get_single_data(), loader.duplicate_keys
    finally:
        loader.dispose()


def _scalar_to_token(val: Any) -> tuple[str, bool]:
    """Return ``(token, ambiguous)`` for one scalar identifier value."""
    if isinstance(val, bool) or val is None:
        return "", False
    if isinstance(val, (_RawScalarInt, _RawScalarFloat)):
        return val.raw_token.strip().casefold(), False
    if isinstance(val, str):
        return val.strip().casefold(), False
    if isinstance(val, (bytes, bytearray)):
        try:
            return bytes(val).decode(_TEXT_ENCODING).strip().casefold(), True
        except UnicodeDecodeError:
            return "", True
    return str(val).strip().casefold(), False  # dates, timestamps


def _coerce_identifier_tokens(raw: Any) -> tuple[list[str], bool, bool]:
    """Return ``(tokens, malformed, ambiguous)`` for one identifier field.

    ``null`` and strings are well formed. Lists and non-string scalars match by
    their string form (malformed). Mapping values and ``!!binary`` are a guess at
    the identity (ambiguous), which degrades the registry.
    """
    if raw is None or isinstance(raw, str):
        tok = raw.strip().casefold() if isinstance(raw, str) else ""
        return ([tok] if tok else []), False, False
    tokens: list[str] = []
    ambiguous = False
    seen: set[int] = set()
    stack: list[Any] = [raw]
    while stack:
        cur = stack.pop()
        if isinstance(cur, _CONTAINERS):
            if id(cur) in seen:
                continue
            seen.add(id(cur))
            if isinstance(cur, Mapping):
                ambiguous = True
                stack.extend(reversed(list(cur.values())))
            else:
                stack.extend(reversed(list(cur)))
            continue
        tok, tok_ambiguous = _scalar_to_token(cur)
        ambiguous = ambiguous or tok_ambiguous
        if tok and tok not in tokens:
            tokens.append(tok)
    return tokens, True, ambiguous


def _extract_personal(node: Mapping[Any, Any]) -> tuple[bool | None, bool]:
    """Return ``(personal_flag, is_malformed)``; non-false values mark personal."""
    if "personal" not in node:
        return None, False
    raw = node["personal"]
    if raw is False or raw is True:
        return raw, False
    return True, True


def _extract_entry(node: Mapping[Any, Any]) -> tuple[FleetEntry | None, bool]:
    """Build one ``FleetEntry`` from a mapping; return ``(entry, ambiguous)``."""
    idents: list[str] = []
    malformed = ambiguous = False
    for key in ENTRY_KEYS:
        if key not in node:
            continue
        tokens, key_malformed, key_ambiguous = _coerce_identifier_tokens(node[key])
        malformed = malformed or key_malformed
        ambiguous = ambiguous or key_ambiguous
        idents.extend(tok for tok in tokens if tok not in idents)
    if not idents:
        return None, ambiguous
    personal, personal_malformed = _extract_personal(node)
    entry = FleetEntry(tuple(idents), personal, malformed or personal_malformed)
    return entry, ambiguous


def _walk_section_nodes(root: Any) -> tuple[list[Mapping[Any, Any]], int, int]:
    """Iteratively collect device mappings.

    Returns ``(candidates, unattributed_bad, attributed_bad)``: cycles and stray
    scalars outside any entry are unattributed; a cycle inside an entry is not.
    """
    candidates: list[Mapping[Any, Any]] = []
    unattributed = attributed = 0
    seen: set[int] = set()
    stack: list[tuple[Any, frozenset[int], bool]] = [(root, frozenset(), False)]
    while stack:
        node, path, in_entry = stack.pop()
        if not isinstance(node, (Mapping, list, tuple)):
            continue
        key = id(node)
        if key in path:
            if in_entry:
                attributed += 1
            else:
                unattributed += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        children: Sequence[Any]
        if isinstance(node, Mapping):
            if any(k in node for k in ENTRY_KEYS):
                candidates.append(node)
                in_entry = True
            children = list(node.values())
        else:
            children = node
            if not in_entry:
                unattributed += sum(1 for item in children if not isinstance(item, _CONTAINERS))
        stack.extend((child, path | {key}, in_entry) for child in reversed(children))
    return candidates, unattributed, attributed


def _record_walk(value: Any, acc: FleetAccumulator) -> tuple[int, int]:
    """Walk *value*, record every entry; return ``(candidates, unattributed_bad)``."""
    candidates, unattributed, attributed = _walk_section_nodes(value)
    for _ in range(unattributed):
        acc.bad()
    for _ in range(attributed):
        acc.bad(identified=True)
    for node in candidates:
        entry, ambiguous = _extract_entry(node)
        if entry is None:
            acc.bad()
        else:
            acc.add(entry, ambiguous=ambiguous)
    return len(candidates), unattributed


def _collect_devices_item(item: Any, acc: FleetAccumulator) -> None:
    if isinstance(item, Mapping):
        found, unattributed = _record_walk(item, acc)
        if found == 0 and unattributed == 0:
            acc.bad()  # a mapping with no serial/id/hardware_id at all
    elif isinstance(item, (list, tuple)):
        _record_walk(item, acc)
        acc.bad()  # `- - serial: X`: nested list, identity attribution is a guess
    else:
        acc.bad()


def collect_section(section: str, value: Any, acc: FleetAccumulator) -> bool:
    """Parse one platform section independently; return whether it was readable."""
    if value is None:
        return True
    if isinstance(value, _DuplicateValues):
        results = [collect_section(section, part, acc) for part in value]
        return any(results)
    if not isinstance(value, (Mapping, list, tuple)):
        acc.bad()
        return False
    if section == "devices" and isinstance(value, list):
        for item in value:
            _collect_devices_item(item, acc)
        return True
    found, unattributed = _record_walk(value, acc)
    if section == "devices":
        acc.bad(identified=found > 0)  # `devices:` should be a list
    return not (unattributed > 0 and found == 0)


def collect_mapping(data: Mapping[Any, Any], acc: FleetAccumulator) -> bool:
    """Collect ``devices:``/``ios:``/``garmin:``; True unless every present one is unreadable."""
    present = [section for section in PLATFORM_SECTIONS if section in data]
    readable = [collect_section(section, data[section], acc) for section in present]
    return not present or any(readable)
