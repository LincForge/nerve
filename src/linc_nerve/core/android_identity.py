"""Android hardware-serial identity: normalize, group transports, resolve aliases.

A wireless bench device (e.g. a Galaxy Watch7 with both an mDNS
``_adb-tls-connect`` transport and a pinned ``<ip>:5555`` transport, or a phone
plugged in over USB while also connected via wifi-adb) appears multiple times
in ``adb devices``. Grouping by a validated ``ro.serialno`` lets discovery list
it once: the preferred transport (USB > ``ip:port`` > ``_adb-tls-connect`` >
other) is the primary serial and the remaining transports are sorted aliases.

Hostile, placeholder, or missing ``ro.serialno`` values normalize to ``""`` and
never merge. Emulators never merge. A shared serial alone is not proof of one
device (clone hardware ships placeholder serials), so transports only merge
when their model matches and at most one of them is a USB link; a refused
merge logs ``android_transport_collapse_refused``.

Pure module: it works on plain values (:class:`Transport`) and on any
dataclass carrying ``serial`` + ``aliases``. It imports nothing from
``android`` / ``android_reliability`` / ``device``; the discovery wiring
(linc-nerve-u2xa) maps the results onto ``NerveDevice``. Every dropped reading is counted by reason
(:func:`rejected_serial_counts`).
"""

from __future__ import annotations

import ipaddress
import re
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

import structlog

from . import adb_health

__all__ = [
    "ALIAS_VIEW_CAP",
    "MAX_HARDWARE_SERIAL_LEN",
    "CollapsedTransport",
    "Transport",
    "alias_view",
    "collapse_transports",
    "find_transport",
    "is_network_transport",
    "mdns_hardware_serial",
    "normalize_hardware_serial",
    "read_hardware_serial",
    "rejected_serial_counts",
    "transport_rank",
    "transport_serials",
]

logger = structlog.get_logger()

MAX_HARDWARE_SERIAL_LEN = 64
# Common unset / clone-hardware placeholders (compared case-insensitively).
_REJECTED_SERIALS = frozenset({"unknown", "0123456789abcdef", "1234567890abcdef"})
_USB_SERIAL_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")
_HOST_PORT_RE = re.compile(r"\A(?:\[[^\]]+\]|[^:\s]+):\d+\Z")
_MDNS_TRANSPORT_RE = re.compile(
    r"\A[^\r\n]+\._[A-Za-z0-9-]+\._(?:tcp|udp)(?:\.local)?\.?\Z",
    re.IGNORECASE,
)
_MDNS_SERIAL_RE = re.compile(
    r"\Aadb-(?P<serial>.+)-[^.-]+\._adb(?:-tls-connect)?\._tcp(?:\.local)?\.?\Z",
    re.IGNORECASE,
)

ALIAS_VIEW_CAP = 64

_REJECTIONS: Counter[str] = Counter()
_rejections_lock = threading.Lock()


class _Addressable(Protocol):
    serial: str
    aliases: list[str]


_ALIAS_VIEWS: OrderedDict[tuple[int, str], tuple[Any, Any]] = OrderedDict()
_alias_views_lock = threading.Lock()


@dataclass(frozen=True)
class Transport:
    """One adb transport as discovery read it: plain values, no device model."""

    serial: str
    hardware_serial: Any = ""  # raw ro.serialno reading; normalized here
    model: str = ""
    is_emulator: bool = False


@dataclass(frozen=True)
class CollapsedTransport:
    """One physical device: ``index`` is the primary transport's input position."""

    index: int
    serial: str
    hardware_serial: str
    aliases: tuple[str, ...] = ()


def _reject(reason: str) -> str:
    """Count one dropped ``ro.serialno`` reading under *reason*; it reads as unknown."""
    with _rejections_lock:
        _REJECTIONS[reason] += 1
    return ""


def rejected_serial_counts() -> dict[str, int]:
    """Snapshot of dropped ``ro.serialno`` readings by reason, process lifetime."""
    with _rejections_lock:
        return dict(_REJECTIONS)


def normalize_hardware_serial(raw: Any) -> str:
    """Normalize a ``getprop ro.serialno`` reading, or ``""`` when unknown/hostile."""
    if not isinstance(raw, str):
        return _reject("not_str")
    val = raw.strip()
    if not val:
        return _reject("empty")
    if len(val) > MAX_HARDWARE_SERIAL_LEN:
        return _reject("too_long")
    if not val.isprintable():
        return _reject("control_char")
    if val.lower() in _REJECTED_SERIALS or not val.strip("0"):
        return _reject("placeholder")
    return val


def read_hardware_serial(shell: Any, hard_timeout: Callable[..., Any], serial: str) -> str:
    """Read ``ro.serialno`` through the injected *hard_timeout*; ``""`` on any failure.

    The read has its own failure path so a timeout here never discards the
    model / OS version / device type the caller already read.
    """
    try:
        raw = hard_timeout(
            shell,
            args=("getprop ro.serialno",),
            kwargs={"timeout": 10},
            timeout=12,
            label=f"discover-serialno-{serial[:8]}",
        )
    except Exception as exc:
        logger.warning("android_serialno_read_failed", serial=serial, error=str(exc))
        return _reject("read_failed")
    return normalize_hardware_serial(raw)


def _is_ip_host(value: str) -> bool:
    """True when *value* is a bare IPv4 or IPv6 host literal (optionally bracketed)."""
    candidate = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    if not candidate:
        return False
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def is_network_transport(transport: str) -> bool:
    """True for a network adb transport (``host:port``, bare IP, ``tcp:``/``udp:``, or mDNS).

    Strips surrounding whitespace so a trailing newline (e.g. ``ip:port\\n``)
    cannot masquerade as a USB serial and bypass verification.
    """
    if not isinstance(transport, str):
        return False
    stripped = transport.strip()
    if not stripped:
        return False
    if stripped[:4].lower() in ("tcp:", "udp:"):
        return True
    if _is_ip_host(stripped):
        return True
    if _HOST_PORT_RE.fullmatch(stripped) or _MDNS_TRANSPORT_RE.fullmatch(stripped):
        return True
    has_ctrl = any(ord(ch) < 32 or ord(ch) == 127 for ch in transport)
    if not has_ctrl:
        return False
    first_token = stripped.split(None, 1)[0]
    return ":" in stripped or "._" in stripped or _is_ip_host(first_token)


def mdns_hardware_serial(transport: str) -> str:
    """Return the ``ro.serialno`` embedded in an mDNS adb-over-wifi transport name.

    Supports both ``adb-<SERIAL>-<suffix>._adb-tls-connect._tcp`` and
    ``adb-<SERIAL>-<suffix>._adb._tcp`` (case-insensitive, optional ``.local.``).
    Any other shape, or a placeholder/hostile serial, returns ``""``.
    """
    if not isinstance(transport, str):
        return ""
    match = _MDNS_SERIAL_RE.match(transport.strip())
    if match is None:
        return ""
    return normalize_hardware_serial(match.group("serial"))


def transport_rank(serial: str) -> int:
    """Preference rank (lower is better): USB (0) > ip:port (1) > _adb-tls-connect (2) > other."""
    if adb_health.is_network_serial(serial):
        return 1
    if "_adb-tls-connect" in serial.lower():
        return 2
    if _USB_SERIAL_RE.fullmatch(serial):
        return 0
    return 3


_Member = tuple[int, Transport]


def _refusal(group: list[_Member], member: _Member) -> str:
    """Why *member* cannot join *group* (``""`` when it can)."""
    transport = member[1]
    if group[0][1].model != transport.model:
        return "model_mismatch"
    if transport_rank(transport.serial) == 0 and any(
        transport_rank(t.serial) == 0 for _, t in group
    ):
        return "second_usb_link"
    return ""


def _place(groups: list[list[_Member]], member: _Member) -> bool:
    """Add *member* to the first compatible group; False when every group refuses."""
    reasons = []
    for group in groups:
        reason = _refusal(group, member)
        if not reason:
            group.append(member)
            return True
        reasons.append(reason)
    if reasons:
        logger.warning(
            "android_transport_collapse_refused",
            serial=member[1].serial,
            model=member[1].model,
            reasons=reasons,
        )
    return False


def _merge_group(hw_serial: str, group: list[_Member]) -> CollapsedTransport:
    index, primary = min(group, key=lambda m: (transport_rank(m[1].serial), m[1].serial))
    aliases = tuple(sorted({t.serial for _, t in group} - {primary.serial}))
    return CollapsedTransport(
        index=index, serial=primary.serial, hardware_serial=hw_serial, aliases=aliases
    )


def collapse_transports(transports: Sequence[Transport]) -> list[CollapsedTransport]:
    """Group transports of one physical device (same valid serial + model), in input order."""
    slots: list[tuple[str, list[_Member]]] = []
    groups: dict[str, list[list[_Member]]] = {}
    for member in enumerate(transports):
        transport = member[1]
        is_emulator = transport.is_emulator or transport.serial.startswith("emulator-")
        hw = "" if is_emulator else normalize_hardware_serial(transport.hardware_serial)
        if hw and _place(groups.setdefault(hw, []), member):
            continue
        group = [member]
        slots.append((hw, group))
        if hw:
            groups[hw].append(group)
    return [_merge_group(hw, group) for hw, group in slots]


def transport_serials(entries: Iterable[_Addressable]) -> list[str]:
    """Every adb transport serial behind *entries*: each primary plus its aliases."""
    return [s for e in entries for s in (e.serial, *e.aliases)]


def find_transport[E: _Addressable](entries: Iterable[E], serial: str) -> E | None:
    """The entry reachable as *serial*: a primary match, else an alias view.

    An entry keeps its other adb transports as ``aliases``; a tool call naming
    one of them must act on THAT transport, so an alias match returns
    ``alias_view(entry, serial)`` rather than the entry itself.
    """
    entries = list(entries)
    entry = next((e for e in entries if e.serial == serial), None)
    if entry is not None:
        return entry
    entry = next((e for e in entries if serial in e.aliases), None)
    return None if entry is None else alias_view(entry, serial)


def alias_view[E: _Addressable](entry: E, serial: str) -> E:
    """*entry* (a dataclass) re-addressed via its alias *serial*, aliases holding the rest.

    The view object is stable per (entry, serial), because the stale-cache
    memo in ``mcp/_core`` matches on identity, and the view cache is
    LRU-bounded at ``ALIAS_VIEW_CAP``. Holding *entry* in the cache keeps its
    ``id`` from being reused by a different object while the view is cached.
    """
    key = (id(entry), serial)
    with _alias_views_lock:
        hit = _ALIAS_VIEWS.get(key)
        if hit is not None and hit[0] is entry:
            _ALIAS_VIEWS.move_to_end(key)
            return hit[1]
        rest = sorted({entry.serial, *entry.aliases} - {serial})
        view = replace(entry, serial=serial, aliases=rest)  # type: ignore[type-var]
        _ALIAS_VIEWS[key] = (entry, view)
        while len(_ALIAS_VIEWS) > ALIAS_VIEW_CAP:
            del _ALIAS_VIEWS[next(iter(_ALIAS_VIEWS))]  # oldest (least recently used)
        return view
