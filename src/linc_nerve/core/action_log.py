"""Per-device action log — a JSONL record of every device-tool call (linc-nerve-7ea).

While an action-log session is active on a serial, every ``@device_tool`` call
against that serial appends one JSON line: the tool, its args (``device``
dropped, secret text redacted), timings relative to the session and to the
active screen recording, the outcome, and — for taps / long presses — the
element under the touch point. The log is what makes an exploratory run
replayable and lines each action up with the video.

Sessions live in-process in :data:`REGISTRY`. The :8111 daemon is the single
long-lived process, so CLI / HTTP callers reach the same session through it;
a session is per-process (per daemon, or per stdio MCP server) and is lost
when that process restarts.

A session opened with a ``target_package`` is GUARDED (linc-nerve-2wh): input
tools refuse while the foreground app is outside the allowed packages, taps on
deny-listed labels (buy, subscribe, send, delete account, ...) are refused, and
app-management tools refuse any package but the target. Each refusal is a
``{"type": "guard"}`` line. The guard fails closed: an unreadable foreground,
or an unreadable, empty or possibly-truncated element tree, refuses the
action. The tree is read fresh and uncapped. A tap — and a swipe Android
delivers as a click: one shorter than :data:`TAP_SLOP_PX`, or one that ends
inside the smallest clickable element holding its start, grown by
:data:`TAP_SLOP_PX` on every side (Android keeps the press within the touch
slop) — is checked against every element containing the point plus
everything inside the smallest clickable element containing it. A longer
swipe is checked against every non-scrollable element holding both its ends
(a "Slide to buy" SeekBar). When a scrollable element holds both ends the
swipe is a scroll, so only a slider (SeekBar / Slider / RatingBar) holding
both ends is checked — the scrolled content's labels are not. An
activation key (ENTER, DPAD_CENTER, SPACE, their numeric codes, gamepad
buttons that fall back to them, ...) is checked against the focused element
and everything inside it, and refused when nothing is focused.
``nerve_type`` text presses keys too: a newline is ENTER, and a space is SPACE
unless a text field holds focus; TAB / ENTER moving focus before a later SPACE
/ ENTER is refused. Labels are folded (:func:`_fold`) so zero-width,
fullwidth, accented and filler spellings match.

The guard is a safety rail that keeps an agent reading untrusted in-app text
from wandering off or spending money — it is NOT a security boundary. Known
gaps, by design or not yet covered:

- ``nerve_actionlog_stop`` is unguarded, so the agent the guard constrains can
  end the session (and the guard with it); it can also act through adb directly.
- ``nerve_install`` is not package-guarded: it takes an artifact path, not a
  package name, so the guard cannot tell what it would install.
- A platform without a foreground API (iOS today) skips the foreground check;
  the label check still runs. (An unresolvable serial is refused.) A
  deep-link ``nerve_launch`` (``uri`` set) is refused there, since iOS opens
  the URI in whichever app claims it. iOS trees carry no focus, so a guarded
  iOS session refuses activation keys and ``nerve_type`` text with a space or
  newline ("no focused element").
- The session is keyed by serial: the same device reached under a second serial
  (USB vs wireless adb) is not guarded.
- Only ``text`` and ``content_desc`` are matched, not ``resource_id``: ids name
  containers too ("message_list", "post_feed"), and every element containing
  the point is checked, so matching ids would refuse taps anywhere in a list.
- UI drawn without an accessibility tree — a WebView, or Flutter / a game
  engine without semantics — exposes no labels, so the label check is blind
  there (a canvas "Buy" button reads as one unlabelled element).
- ENTER typed or pressed into a focused text field runs the field's IME
  action: in a chat composer that is "send". The label check sees only the
  field (its text / hint), not the send button the action triggers.
- Lookalike characters are not folded: a Cyrillic / Greek homoglyph ("Buy"
  spelled with U+0412 CYRILLIC CAPITAL VE, "Send" with U+0435 CYRILLIC IE), a
  braille blank (U+2800) or a no-break space (U+00A0, which NFKD turns into a
  plain space) inside a word all evade the deny match.
- A swipe-to-confirm control whose label lacks a deny word ("Swipe up to
  submit") is not caught, and neither is a swipe that leaves the control
  (only a swipe with both ends on it is checked).

Clock seams: ``_clock`` (monotonic seconds) and ``_wallclock`` (aware UTC
datetime) are module-level so tests can pin time without sleeping.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import threading
import time
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast

import structlog

from linc_nerve.core import observability

log = structlog.get_logger().bind(domain="action_log")

HEADER_VERSION = 1
REDACTED = "<redacted>"

# Monotonic seconds for offsets; wall clock only for the human-readable header.
_clock = time.monotonic


def _wallclock() -> datetime:
    return datetime.now(UTC)


# Device tools that observe but never change the device — logged (they mark
# where the agent looked) with ``"readonly": true``.
READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "nerve_screenshot",
        "nerve_stream_screenshot",
        "nerve_elements",
        "nerve_state",
        "nerve_diff",
        "nerve_density",
        "nerve_apps",
        "nerve_logcat",
        "nerve_inspect",
        "nerve_stream_logs",
        "nerve_network",
    }
)

# Tools whose (x, y) args get the element under the point recorded as ``target``.
TARGET_TOOLS: frozenset[str] = frozenset({"nerve_tap", "nerve_long_press"})

# The session-control tools themselves are never logged. nerve_replay is not
# logged either: the device tools it dispatches log one line per replayed step.
UNLOGGED_TOOLS: frozenset[str] = frozenset(
    {"nerve_actionlog_start", "nerve_actionlog_stop", "nerve_replay"}
)


# Sentinel: "use the recording mark live at append time".
_LIVE = object()


class ActionLogError(RuntimeError):
    """A session-state precondition failed (already active / none active)."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def parse_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    """Parse a uiautomator/WDA bounds string ``"[x1,y1][x2,y2]"``; None if malformed."""
    if not isinstance(bounds, str):
        return None
    match = _BOUNDS_RE.fullmatch(bounds.strip())
    if match is None:
        return None
    x1, y1, x2, y2 = (int(g) for g in match.groups())
    return x1, y1, x2, y2


def _as_element_dict(element: Any) -> dict[str, Any] | None:
    if isinstance(element, dict):
        return element
    to_dict = getattr(element, "to_dict", None)
    return to_dict() if callable(to_dict) else None


def _iter_elements(elements: Iterable[Any]) -> Iterator[dict[str, Any]]:
    """Yield every element dict, depth-first through any ``children``."""
    for element in elements:
        as_dict = _as_element_dict(element)
        if as_dict is None:
            continue
        yield as_dict
        yield from _iter_elements(as_dict.get("children") or [])


def _first(element: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = element.get(key)
        if value:
            return str(value)
    return ""


def _target_fields(element: dict[str, Any]) -> dict[str, str]:
    """Normalise compact (``id``/``desc``/``class``) or long field names."""
    return {
        "resource_id": _first(element, "resource_id", "id"),
        "text": _first(element, "text"),
        "content_desc": _first(element, "content_desc", "desc"),
        "class_name": _first(element, "class_name", "class"),
        "bounds": _first(element, "bounds"),
    }


def _area_if_contains(element: dict[str, Any], x: int, y: int) -> int | None:
    rect = parse_bounds(element.get("bounds", ""))
    if rect is None:
        return None
    x1, y1, x2, y2 = rect
    if not (x1 <= x <= x2 and y1 <= y <= y2):
        return None
    return (x2 - x1) * (y2 - y1)


def _containing(flat: list[dict[str, Any]], x: int, y: int) -> list[dict[str, Any]]:
    """Elements of *flat* whose bounds contain (x, y), smallest area first (stable)."""
    hits = [(area, el) for el in flat if (area := _area_if_contains(el, x, y)) is not None]
    return [el for _, el in sorted(hits, key=lambda hit: hit[0])]


def elements_at(elements: Iterable[Any], x: int, y: int) -> list[dict[str, str]]:
    """Every element whose bounds contain (x, y), smallest area first."""
    return [_target_fields(el) for el in _containing(list(_iter_elements(elements)), x, y)]


def element_at(elements: Iterable[Any], x: int, y: int) -> dict[str, str] | None:
    """The smallest-area element whose bounds contain (x, y), or None."""
    hits = elements_at(elements, x, y)
    return hits[0] if hits else None


def _within(element: dict[str, Any], outer: tuple[int, int, int, int]) -> bool:
    rect = parse_bounds(element.get("bounds", ""))
    if rect is None:
        return False
    return (
        outer[0] <= rect[0] and outer[1] <= rect[1] and rect[2] <= outer[2] and rect[3] <= outer[3]
    )


def _first_clickable(hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first clickable element of *hits* (smallest first: the innermost one)."""
    return next((el for el in hits if el.get("clickable")), None)


def guard_labels(elements: Iterable[Any], x: int, y: int) -> list[dict[str, str]]:
    """Every element a guarded tap at (x, y) could act on, for the label check.

    That is each element containing the point (an unlabelled icon inside a
    "Subscribe" button), plus every element lying inside the smallest
    clickable element containing the point (the padding of a clickable row
    around its "Buy" text).
    """
    flat = list(_iter_elements(elements))
    hits = _containing(flat, x, y)
    container = _first_clickable(hits)
    outer = None if container is None else parse_bounds(container.get("bounds", ""))
    inner = [] if outer is None else [el for el in flat if el not in hits and _within(el, outer)]
    return [_target_fields(el) for el in (*hits, *inner)]


def _secret_text(args: dict[str, Any]) -> str | None:
    text = args.get("text")
    if args.get("secret") is True and isinstance(text, str):
        return text
    return None


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Copy *args* without ``device``; secret text collapses to its length."""
    secret = _secret_text(args)
    if secret is not None:
        # "secret" is the boolean redaction flag, not a credential (bandit B105).
        return {"text": REDACTED, "secret": True, "length": len(secret)}  # nosec B105
    return {key: value for key, value in args.items() if key != "device"}


def _scrub(error: str | None, args: dict[str, Any]) -> str | None:
    secret = _secret_text(args)
    if error is None or not secret:
        return error
    return error.replace(secret, REDACTED)


def outcome_of(result: Any) -> tuple[bool, str | None]:
    """(ok, error) for a tool result — an ``error`` key in a dict means failure."""
    if isinstance(result, dict) and "error" in result:
        return False, str(result["error"])
    return True, None


def build_action_entry(
    tool: str,
    args: dict[str, Any],
    *,
    ok: bool,
    error: str | None,
    duration_ms: float,
    readonly: bool = False,
    target: dict[str, str] | None = None,
    with_target: bool = False,
) -> dict[str, Any]:
    """The tool-specific part of an action line (``append`` stamps seq + times)."""
    entry: dict[str, Any] = {
        "tool": tool,
        "args": redact_args(args),
        "duration_ms": round(duration_ms, 3),
        "ok": ok,
        "error": _scrub(error, args),
    }
    if readonly:
        entry["readonly"] = True
    if with_target:
        entry["target"] = target
    return entry


def _to_line(record: dict[str, Any]) -> str:
    return json.dumps(record, default=str, ensure_ascii=False) + "\n"


def actionlog_roots(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Allowed host directories for action logs from NERVE_ACTIONLOG_ROOTS or default LINC_HOME."""
    env = os.environ if environ is None else environ
    raw = env.get("NERVE_ACTIONLOG_ROOTS", "")
    if raw.strip():
        roots: list[Path] = []
        for part in raw.split(os.pathsep):
            part = part.strip()
            if not part:
                continue
            path = Path(part).expanduser()
            if path.is_absolute():
                roots.append(path)
        if roots:
            return roots
    return [observability.linc_home() / "nerve" / "actionlogs"]


def resolve_log_path(path: str, roots: Sequence[Path] | Iterable[Path]) -> Path | str:
    """Resolve an action-log path against allowed roots, or return an error message."""
    if "\x00" in path:
        return "path cannot contain NUL byte"
    candidate = Path(path).expanduser()
    if (
        candidate.name in ("..", ".")
        or not path.endswith(".jsonl")
        or candidate.suffix != ".jsonl"
    ):
        return f"path must be a .jsonl file, got {path!r}"
    roots_list = list(roots)
    if not roots_list:
        return "no action-log roots configured"
    if not candidate.is_absolute():
        candidate = roots_list[0] / candidate
    parent_real = Path(os.path.realpath(candidate.parent))
    candidate_resolved = parent_real / candidate.name
    for root in roots_list:
        root_real = Path(os.path.realpath(root))
        if parent_real == root_real or parent_real.is_relative_to(root_real):
            return candidate_resolved
    roots_str = ", ".join(str(r) for r in roots_list)
    return f"path must be under an allowed action-log root ({roots_str}), got {path!r}"


# O_NONBLOCK: a FIFO named *.jsonl fails at once (ENXIO with no reader, or the
# regular-file check below) instead of blocking the tool worker in open().
_APPEND_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
)


def _require_regular(fd: int, path: Path) -> None:
    """Close *fd* and raise unless it is a regular file (never a FIFO / device)."""
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"not a regular file: {path}")


def _open_append(path: Path) -> IO[str]:
    """Open *path* for append, creating parents; a new file is owner-only (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, _APPEND_FLAGS, 0o600)
    _require_regular(fd, path)
    return os.fdopen(fd, "a", encoding="utf-8")


# ---------------------------------------------------------------------------
# Guard — pure helpers (linc-nerve-2wh)
# ---------------------------------------------------------------------------

# Packages a guarded session always lets the agent act in, on top of the target
# and ``allow_packages``: the runtime-permission dialogs and package installer
# an app raises during normal use (AOSP, Google and Samsung builds), and
# ``android`` itself, which owns system dialogs (ANR, chooser, ...).
BUILTIN_ALLOWED_PACKAGES: frozenset[str] = frozenset(
    {
        "com.google.android.permissioncontroller",
        "com.android.permissioncontroller",
        "com.google.android.packageinstaller",
        "com.samsung.android.permissioncontroller",
        "android",
    }
)

# Labels a guarded tap / long press refuses — spending money, posting or
# messaging on the user's behalf, and destroying the account.
DEFAULT_DENY_WORDS: tuple[str, ...] = (
    "buy",
    "purchase",
    "subscribe",
    "upgrade",
    "pay",
    "checkout",
    "order",
    "donate",
    "post",
    "publish",
    "send",
    "share",
    "invite",
    "follow",
    "message",
    "call",
    "delete account",
    "delete my account",
    "remove account",
    "report",
)

# Input tools that only run while the foreground app is allowed.
INPUT_GUARD_TOOLS: frozenset[str] = frozenset(
    {"nerve_tap", "nerve_long_press", "nerve_swipe", "nerve_type", "nerve_key"}
)

# App-management tools that only run against the target package.
PACKAGE_GUARD_TOOLS: frozenset[str] = frozenset(
    {"nerve_launch", "nerve_uninstall", "nerve_clear_app_data", "nerve_grant_permission"}
)

# Keys that always run, so an agent can always get back out. A bare number is
# compared as an integer (``input keyevent 03`` is HOME), but KEYCODE_3 /
# KEYCODE_4 are the digit keys, so a prefixed number never escapes.
ESCAPE_KEY_NAMES: frozenset[str] = frozenset({"BACK", "HOME"})
ESCAPE_KEY_CODES: frozenset[int] = frozenset({3, 4})

# Keys that activate the focused element — a tap by another name. Names take an
# optional KEYCODE_ prefix. Numbers (bare or KEYCODE_-prefixed, which API <= 28
# parses as a number) compare as integers, so "066" and "KEYCODE_66" are ENTER.
# Besides ENTER / DPAD_CENTER / NUMPAD_ENTER / SPACE this covers the keys whose
# key-character map falls back to one of them when the app leaves them
# unhandled (Virtual.kcm, used by ``input keyevent``, and Generic.kcm, read on
# an API 35 device): NUMPAD_5 and the gamepad buttons A, C, X, Y, Z, THUMBL,
# THUMBR, START and 1-16. SELECT and MODE (MENU / HOME there) are classified
# fail closed, as older AOSP maps send them to SPACE / ENTER.
ACTIVATION_KEY_NAMES: frozenset[str] = frozenset(
    {
        *("ENTER", "DPAD_CENTER", "NUMPAD_ENTER", "SPACE", "NUMPAD_5"),
        *("BUTTON_A", "BUTTON_C", "BUTTON_X", "BUTTON_Y", "BUTTON_Z"),
        *("BUTTON_THUMBL", "BUTTON_THUMBR", "BUTTON_START", "BUTTON_SELECT", "BUTTON_MODE"),
        *(f"BUTTON_{n}" for n in range(1, 17)),
    }
)
ACTIVATION_KEY_CODES: frozenset[int] = frozenset(
    {66, 23, 160, 62, 149, 96, 98, 99, 100, 101, 106, 107, 108, 109, 110, *range(188, 204)}
)

# A swipe this short (px, start to end) lands as a click on Android.
TAP_SLOP_PX = 48

# Element-tree reads for the guard lift the 80-node cap and look this deep.
GUARD_MAX_DEPTH = 100
# The cap a reader without ``max_nodes`` (iOS) applies; a tree this long may be cut.
ELEMENT_CAP = 80


def _normalise_phrase(text: str) -> str:
    return " ".join(text.lower().split())


# Hangul fillers render blank but are letters (Lo), so they survive the category drop.
_INVISIBLE_LETTERS = frozenset("\u115f\u1160\u3164\uffa0")
# Format (zero-width, soft hyphen, ...) and combining marks (accents, CGJ).
_DROPPED_CATEGORIES = frozenset({"Cf", "Mn", "Me"})


def _kept(ch: str) -> bool:
    return ch not in _INVISIBLE_LETTERS and unicodedata.category(ch) not in _DROPPED_CATEGORIES


def _fold(text: str) -> str:
    """Decompose (NFKD), drop invisible / combining characters, recompose (NFC).

    Defeats zero-width, fullwidth, accented and Hangul-filler spellings of a
    deny word ("B\u200buy", "\uff22\uff35\uff39", "Pa\u00ff", "B\u3164uy").
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return unicodedata.normalize("NFC", "".join(filter(_kept, decomposed)))


def _phrase_group(index: int, phrase: str) -> str:
    """One named alternative; the name carries the phrase (hex) for denied_label."""
    body = r"[\W_]*".join(map(re.escape, phrase.split()))
    return f"(?P<p{index}_{phrase.encode().hex()}>{body})"


def _group_phrase(name: str) -> str:
    return bytes.fromhex(name.partition("_")[2]).decode()


def deny_pattern(words: Iterable[str]) -> re.Pattern[str]:
    """Case-insensitive, word-boundary regex over *words* (longest phrase first).

    Phrase words may be joined by any run of non-letters ("Delete-account",
    "Delete_account", "Deleteaccount" once a zero-width space is dropped).
    """
    phrases = sorted({_normalise_phrase(w) for w in words if w.strip()}, key=len, reverse=True)
    if not phrases:
        return re.compile(r"(?!)")  # matches nothing
    alternation = "|".join(_phrase_group(i, p) for i, p in enumerate(phrases))
    return re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)


_DEFAULT_DENY = deny_pattern(DEFAULT_DENY_WORDS)


def denied_label(text: str | None, pattern: re.Pattern[str] = _DEFAULT_DENY) -> str | None:
    """The canonical deny word/phrase *text* contains (lower-cased), or None.

    *text* is folded (:func:`_fold`) before matching; the phrase returned is
    the configured one, never the matched spelling.
    """
    if not text:
        return None
    match = pattern.search(_fold(text))
    if match is None or match.lastgroup is None:
        return None
    return _group_phrase(match.lastgroup)


def split_csv(value: str) -> list[str]:
    """Non-empty, stripped items of a comma-separated string."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


@dataclass(frozen=True)
class GuardConfig:
    """What a guarded session allows: foreground packages and tap labels."""

    target_package: str
    allowed_packages: frozenset[str]
    deny_words: tuple[str, ...]
    pattern: re.Pattern[str] = field(compare=False, repr=False)

    def describe(self) -> dict[str, Any]:
        return {
            "target_package": self.target_package,
            "allowed_packages": sorted(self.allowed_packages),
            "deny_words": list(self.deny_words),
        }


def build_guard(
    target_package: str, allow_packages: str = "", deny_words: str = ""
) -> GuardConfig | None:
    """A guard for *target_package* (None when empty — the session is unguarded).

    ``allow_packages`` / ``deny_words`` are comma lists; non-empty
    ``deny_words`` replaces :data:`DEFAULT_DENY_WORDS`.
    """
    if not target_package:
        return None
    words = tuple(split_csv(deny_words)) or DEFAULT_DENY_WORDS
    allowed = frozenset({target_package, *split_csv(allow_packages)}) | BUILTIN_ALLOWED_PACKAGES
    return GuardConfig(target_package, allowed, words, deny_pattern(words))


def refusal(reason: str, detail: str, message: str) -> dict[str, Any]:
    """The tool result for a guard refusal (a validation error, not a tool failure)."""
    return {"error": message, "guard": reason, "detail": detail, "validation_error": True}


def package_refusal(guard: GuardConfig, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Refuse an app-management tool aimed at any package but the target."""
    if tool not in PACKAGE_GUARD_TOOLS:
        return None
    package = str(args.get("package", ""))
    if package == guard.target_package:
        return None
    message = (
        f"guarded session: {tool} on {package} refused, only {guard.target_package} is allowed"
    )
    return refusal("other_package", package, message)


# Platforms whose deep-link launch stays inside the named package: Android runs
# ``am start -a VIEW -d <uri> <package>``, and the trailing package restricts
# the intent to it. iOS (``simctl openurl`` / WDA ``/url``) ignores the bundle
# id and opens the URI in whichever app claims it.
URI_CONFINED_PLATFORMS: frozenset[str] = frozenset({"android"})


def is_uri_launch(tool: str, args: dict[str, Any]) -> bool:
    """Whether *tool* is a deep-link launch (``nerve_launch`` with a ``uri``)."""
    return tool == "nerve_launch" and bool(args.get("uri"))


def uri_launch_refusal(guard: GuardConfig, platform: str) -> dict[str, Any] | None:
    """Refuse a guarded deep-link launch the platform does not confine to the target."""
    if platform in URI_CONFINED_PLATFORMS:
        return None
    detail = f"uri launch on {platform or 'unknown'} is not confined to {guard.target_package}"
    return refusal("other_package", detail, f"guarded session: nerve_launch refused, {detail}")


def _key_token(key: Any) -> str | None:
    """*key* upper-cased and stripped, or None when it is not a string."""
    return key.strip().upper() if isinstance(key, str) else None


def _key_number(token: str) -> int | None:
    """The integer an all-digit *token* names ("066" -> 66), else None."""
    return int(token) if token.isdecimal() else None


def is_escape_key(key: Any) -> bool:
    """BACK / HOME (any case, optional ``KEYCODE_`` prefix), or a bare 3 / 4 ("03")."""
    token = _key_token(key)
    if token is None:
        return False
    return token.removeprefix("KEYCODE_") in ESCAPE_KEY_NAMES or (
        _key_number(token) in ESCAPE_KEY_CODES
    )


def is_activation_key(key: Any) -> bool:
    """A key that activates the focused element (see :data:`ACTIVATION_KEY_NAMES`)."""
    token = _key_token(key)
    if token is None:
        return False
    name = token.removeprefix("KEYCODE_")
    return name in ACTIVATION_KEY_NAMES or _key_number(name) in ACTIVATION_KEY_CODES


def _ints(args: dict[str, Any], *keys: str) -> tuple[int, ...] | None:
    """``int(args[key])`` for each key, or None if any is missing / not a number."""
    try:
        return tuple(int(args[key]) for key in keys)
    except (KeyError, TypeError, ValueError):
        return None


def touch_point(tool: str, args: dict[str, Any]) -> tuple[int, ...] | None:
    """Where *tool* would click: a tap / long press point, or a short swipe's start.

    A swipe within :data:`TAP_SLOP_PX` of its start is a click on Android.
    """
    if tool in TARGET_TOOLS:
        return _ints(args, "x", "y")
    coords = _ints(args, "start_x", "start_y", "end_x", "end_y") if tool == "nerve_swipe" else None
    if coords is None:
        return None
    start_x, start_y, end_x, end_y = coords
    return (
        (start_x, start_y) if math.hypot(end_x - start_x, end_y - start_y) <= TAP_SLOP_PX else None
    )


def tap_target(tool: str, args: dict[str, Any], elements: Iterable[Any]) -> dict[str, str] | None:
    """The element under a tap / long press point (the action log's ``target``)."""
    point = touch_point(tool, args) if tool in TARGET_TOOLS else None
    return None if point is None else element_at(elements, *point)


_SWIPE_KEYS = ("start_x", "start_y", "end_x", "end_y")

# What ``input text`` turns into key presses: "\n" / "\r" -> ENTER, and " "
# (sent as "%s", which a literal "%s" also becomes) -> SPACE.
_TYPED_ENTER_RE = re.compile(r"[\n\r]")
_TYPED_SPACE_RE = re.compile(r" |%s")
# TAB / ENTER move focus off the field, so a later SPACE / ENTER in the same
# text lands on whatever got focus next.
_TYPED_FOCUS_MOVE_RE = re.compile(r"[\t\n\r].*?(?: |%s|[\n\r])", re.DOTALL)
# Classes a swipe drags (and so activates) even inside a scrollable container.
_SLIDER_CLASS_RE = re.compile(r"SeekBar|Slider|RatingBar")
# Classes that take a typed SPACE as text rather than as a click (Android
# EditText and subclasses, iOS text fields / views).
_TEXT_INPUT_CLASS_RE = re.compile(
    r"(?:EditText|AutoCompleteTextView|XCUIElementType(?:Secure)?TextField"
    r"|XCUIElementTypeSearchField|XCUIElementTypeTextView)$"
)


def _typed_text(args: dict[str, Any]) -> str:
    text = args.get("text")
    return text if isinstance(text, str) else ""


def _types_keys(text: str) -> bool:
    """Whether typing *text* presses ENTER or SPACE."""
    return bool(_TYPED_ENTER_RE.search(text) or _TYPED_SPACE_RE.search(text))


def needs_label_check(tool: str, args: dict[str, Any]) -> bool:
    """Whether a guarded *tool* call may activate an element (and needs the tree).

    Taps, every swipe (a short one is a click; a long one may stay inside one
    button), activation keys, and text that types ENTER / SPACE.
    """
    if tool == "nerve_key":
        return is_activation_key(args.get("key"))
    if tool == "nerve_type":
        return _types_keys(_typed_text(args))
    if tool == "nerve_swipe":
        return _ints(args, *_SWIPE_KEYS) is not None
    return touch_point(tool, args) is not None


def _in_button_swipe_start(
    args: dict[str, Any], elements: Iterable[Any]
) -> tuple[int, int] | None:
    """A swipe's start when it ends inside the smallest clickable holding the start.

    Android delivers such a swipe as a click on that element (nothing
    scrolls it away), however long it is.
    """
    coords = _ints(args, *_SWIPE_KEYS)
    if coords is None:
        return None
    start_x, start_y, end_x, end_y = coords
    container = _first_clickable(_containing(list(_iter_elements(elements)), start_x, start_y))
    rect = None if container is None else parse_bounds(container.get("bounds", ""))
    if rect is None or not _in_slop(rect, end_x, end_y):
        return None
    return start_x, start_y


def _in_slop(rect: tuple[int, int, int, int], x: int, y: int) -> bool:
    """Whether (x, y) lies in *rect* grown by :data:`TAP_SLOP_PX` on every side."""
    x1, y1, x2, y2 = rect
    return x1 - TAP_SLOP_PX <= x <= x2 + TAP_SLOP_PX and y1 - TAP_SLOP_PX <= y <= y2 + TAP_SLOP_PX


def _is_slider(element: dict[str, Any]) -> bool:
    return bool(_SLIDER_CLASS_RE.search(_target_fields(element)["class_name"]))


def _span_activatable(spanning: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The elements of *spanning* (all holding both swipe ends) a swipe may activate.

    A scrollable among them makes the swipe a scroll: only a slider is
    activated then, not the scrolled content. Otherwise every non-scrollable
    element is.
    """
    if any(el.get("scrollable") for el in spanning):
        return [el for el in spanning if not el.get("scrollable") and _is_slider(el)]
    return spanning


def swipe_span_labels(args: dict[str, Any], elements: Iterable[Any]) -> list[dict[str, str]]:
    """Elements a long swipe may activate — those holding both its ends, smallest first.

    A swipe across one control (a "Slide to buy" SeekBar) activates it. When a
    scrollable element holds both ends the swipe is a scroll: only a slider
    holding both ends counts, not the scrolled content ("share this post"
    text, an "Upgrade to Pro" pager page) nor the scrollable itself.
    """
    coords = _ints(args, *_SWIPE_KEYS)
    if coords is None or math.dist(coords[:2], coords[2:]) <= TAP_SLOP_PX:
        return []
    start_x, start_y, end_x, end_y = coords
    hits = _containing(list(_iter_elements(elements)), start_x, start_y)
    spanning = [el for el in hits if _area_if_contains(el, end_x, end_y) is not None]
    return [_target_fields(el) for el in _span_activatable(spanning)]


def click_point(
    tool: str, args: dict[str, Any], elements: Iterable[Any]
) -> tuple[int, ...] | None:
    """Where a tap, long press or click-like swipe lands, or None."""
    point = touch_point(tool, args)
    if point is not None or tool != "nerve_swipe":
        return point
    return _in_button_swipe_start(args, elements)


def _focused(elements: Iterable[Any]) -> list[dict[str, Any]]:
    return [el for el in _iter_elements(elements) if el.get("focused")]


def _is_text_input(element: dict[str, Any]) -> bool:
    return bool(_TEXT_INPUT_CLASS_RE.search(_target_fields(element)["class_name"]))


def _types_into_text_input(elements: Iterable[Any]) -> bool:
    """Whether focus is held, and only by text inputs (a SPACE there is text)."""
    focused = _focused(elements)
    return bool(focused) and all(map(_is_text_input, focused))


def _presses_activation(tool: str, args: dict[str, Any], elements: Iterable[Any]) -> bool:
    """Whether *tool* presses a key that activates the focused element."""
    if tool == "nerve_key":
        return is_activation_key(args.get("key"))
    text = _typed_text(args) if tool == "nerve_type" else ""
    if _TYPED_ENTER_RE.search(text):
        return True
    return bool(_TYPED_SPACE_RE.search(text)) and not _types_into_text_input(elements)


def _focus_refusal(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Refuse text that moves focus (TAB / ENTER) before a later SPACE / ENTER."""
    if tool != "nerve_type" or not _TYPED_FOCUS_MOVE_RE.search(_typed_text(args)):
        return None
    detail = "focus moves before an activation key"
    return refusal("target_unreadable", detail, f"guarded session: input refused, {detail}")


def no_focus_refusal() -> dict[str, Any]:
    """Refuse an activation key with nothing focused (it may focus and press the first view)."""
    detail = "no focused element"
    return refusal("target_unreadable", detail, f"guarded session: input refused, {detail}")


def activation_refusal(
    guard: GuardConfig, tool: str, args: dict[str, Any], elements: list[Any]
) -> dict[str, Any] | None:
    """The refusal for a guarded call that would activate a deny-labelled element, or None.

    A click is checked with :func:`guard_labels` at its point, a longer swipe
    with :func:`swipe_span_labels` (a scroll only against a slider it spans);
    an activation key (pressed or typed) with :func:`focus_labels`, refused
    outright when nothing is focused or when the text moves focus before
    pressing it.
    """
    point = click_point(tool, args, elements)
    if point is not None:
        return label_refusal(guard, guard_labels(elements, *point))
    if tool == "nerve_swipe":
        return label_refusal(guard, swipe_span_labels(args, elements))
    focus_moves = _focus_refusal(tool, args)
    if focus_moves is not None or not _presses_activation(tool, args, elements):
        return focus_moves
    labels = focus_labels(elements)
    return label_refusal(guard, labels) if labels else no_focus_refusal()


def focus_labels(elements: Iterable[Any]) -> list[dict[str, str]]:
    """Every focused element and everything inside it — what an activation key hits."""
    flat = list(_iter_elements(elements))
    labels: list[dict[str, str]] = []
    for focused in (el for el in flat if el.get("focused")):
        outer = parse_bounds(focused.get("bounds", ""))
        inner = (
            []
            if outer is None
            else [el for el in flat if el is not focused and _within(el, outer)]
        )
        labels += [_target_fields(el) for el in (focused, *inner)]
    return labels


def tree_problem(elements: list[Any], capped: bool) -> str | None:
    """Why a guarded read of *elements* cannot be trusted, or None.

    An empty tree is a screen still loading, not "nothing under the point";
    a *capped* reader returning :data:`ELEMENT_CAP` nodes may have cut the rest.
    """
    if not elements:
        return "empty element tree"
    if capped and len(elements) >= ELEMENT_CAP:
        return f"element tree truncated at {ELEMENT_CAP} nodes"
    return None


def needs_foreground(tool: str, args: dict[str, Any]) -> bool:
    """Whether *tool* must check the foreground app first (BACK/HOME never do)."""
    if tool not in INPUT_GUARD_TOOLS:
        return False
    return not (tool == "nerve_key" and is_escape_key(args.get("key")))


def foreground_refusal(guard: GuardConfig, package: str) -> dict[str, Any] | None:
    """Refuse when *package* (``""`` = unreadable) is not an allowed foreground."""
    if package in guard.allowed_packages:
        return None
    shown = package or "unknown"
    message = f"guarded session: foreground {shown} is outside the allowed apps"
    return refusal("foreground_not_allowed", shown, message)


def _denied_word(guard: GuardConfig, labels: Iterable[dict[str, str]]) -> str | None:
    for label in labels:
        for key in ("text", "content_desc"):
            word = denied_label(label.get(key), guard.pattern)
            if word is not None:
                return word
    return None


def label_refusal(
    guard: GuardConfig | None, labels: Iterable[dict[str, str]]
) -> dict[str, Any] | None:
    """Refuse a tap when any of *labels* (see :func:`guard_labels`) hits a deny word."""
    word = None if guard is None else _denied_word(guard, labels)
    if word is None:
        return None
    return refusal("denied_label", word, f"guarded session: input on a {word!r} label refused")


def unreadable_refusal(detail: str) -> dict[str, Any]:
    """Refuse a guarded tap / activation whose element tree could not be read (fail closed)."""
    message = f"guarded session: input refused, the element tree could not be read ({detail})"
    return refusal("target_unreadable", detail, message)


def build_guard_entry(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    """The tool-specific part of a ``guard`` line for a refusal *result*."""
    return {"type": "guard", "tool": tool, "reason": result["guard"], "detail": result["detail"]}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class ActionLogSession:
    """One open JSONL file for one serial; every write is flushed."""

    def __init__(
        self, serial: str, path: Path, meta: dict[str, Any], guard: GuardConfig | None = None
    ) -> None:
        self.serial = serial
        self.path = path
        self.meta = meta
        self.guard = guard
        self.guard_refusals = 0
        self.started = _clock()
        self.started_at = _wallclock().isoformat()
        self.seq = 0
        self._lock = threading.Lock()
        self._fh = _open_append(path)

    def header(self) -> dict[str, Any]:
        header = {
            "type": "header",
            "version": HEADER_VERSION,
            "serial": self.serial,
            "started_at": self.started_at,
            "meta": self.meta,
        }
        if self.guard is not None:
            header["guard"] = self.guard.describe()
        return header

    def write(self, record: dict[str, Any]) -> None:
        self._fh.write(_to_line(record))
        self._fh.flush()

    def write_action(
        self, entry: dict[str, Any], at: float, rec_started: float | None
    ) -> dict[str, Any] | None:
        with self._lock:
            record = {
                "type": "action",
                "seq": self.seq + 1,
                **entry,
                "t_session_s": round(at - self.started, 3),
                "t_rec_s": None if rec_started is None else round(at - rec_started, 3),
            }
            try:
                self.write(record)
            except (OSError, ValueError) as exc:  # closed by a racing stop
                log.warning("action_log_write_failed", serial=self.serial, error=str(exc))
                return None
            self.seq += 1
            return record

    def write_guard(self, entry: dict[str, Any], at: float, rec_started: float | None) -> None:
        """Append one ``guard`` refusal line and count it."""
        with self._lock:
            record = {
                **entry,
                "t_session_s": round(at - self.started, 3),
                "t_rec_s": None if rec_started is None else round(at - rec_started, 3),
            }
            self.guard_refusals += 1
            try:
                self.write(record)
            except (OSError, ValueError) as exc:  # closed by a racing stop
                log.warning("action_log_write_failed", serial=self.serial, error=str(exc))

    def stop_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"serial": self.serial, "path": str(self.path), "entries": self.seq}
        if self.guard is not None:
            info["guard_refusals"] = self.guard_refusals
        return info

    def close(self) -> None:
        with self._lock:
            self._fh.close()


class ActionLogRegistry:
    """Thread-safe serial → session map, plus per-serial recording start times."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, ActionLogSession] = {}
        self._rec_started: dict[str, float] = {}

    def start(
        self,
        serial: str,
        path: Path,
        meta: dict[str, Any] | None = None,
        guard: GuardConfig | None = None,
    ) -> dict[str, Any]:
        path = Path(path)
        with self._lock:
            if serial in self._sessions:
                active = self._sessions[serial].path
                raise ActionLogError(f"action log already active on {serial}: {active}")
            session = ActionLogSession(serial, path, dict(meta or {}), guard)
            session.write(session.header())
            self._sessions[serial] = session
        log.info("action_log_started", serial=serial, path=str(path))
        return {"serial": serial, "path": str(path), "started_at": session.started_at}

    def stop(self, serial: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.pop(serial, None)
        if session is None:
            raise ActionLogError(f"no action log active on {serial}")
        session.close()
        log.info("action_log_stopped", serial=serial, entries=session.seq)
        return session.stop_info()

    def active(self, serial: str) -> ActionLogSession | None:
        return self._sessions.get(serial)

    def mark_recording_started(self, serial: str, t: float) -> None:
        with self._lock:
            self._rec_started[serial] = t

    def mark_recording_stopped(self, serial: str) -> None:
        with self._lock:
            self._rec_started.pop(serial, None)

    def recording_offset(self, serial: str, now: float) -> float | None:
        started = self._rec_started.get(serial)
        return None if started is None else now - started

    def recording_started(self, serial: str) -> float | None:
        """Monotonic start of the recording live on *serial*, or None."""
        return self._rec_started.get(serial)

    def append(
        self,
        serial: str,
        entry: dict[str, Any],
        at: float | None = None,
        *,
        rec_started: float | None | object = _LIVE,
    ) -> dict[str, Any] | None:
        """Append one action line; a no-op (None) when no session is active.

        ``at`` (default: now) is the action's start time. ``rec_started`` is the
        recording start in effect at ``at`` — callers snapshot it before the
        action so a call that starts/stops a recording is timed against the
        recording that was live when it began. Default: the current mark.
        """
        session = self.active(serial)
        if session is None:
            return None
        now = _clock() if at is None else at
        live = rec_started is _LIVE
        rec = self.recording_started(serial) if live else cast("float | None", rec_started)
        return session.write_action(entry, now, rec)

    def record_guard(self, serial: str, tool: str, result: dict[str, Any]) -> None:
        """Write a ``guard`` line for refusal *result*; a no-op with no session."""
        session = self.active(serial)
        if session is None:
            return
        log.info("action_log_guard_refusal", serial=serial, tool=tool, reason=result["guard"])
        entry = build_guard_entry(tool, result)
        session.write_guard(entry, _clock(), self.recording_started(serial))

    def reset(self) -> None:
        """Close every session and forget recording marks (tests / shutdown)."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._rec_started.clear()
        for session in sessions:
            session.close()


REGISTRY = ActionLogRegistry()
