"""Replay an action log — re-run its steps with element locators (linc-nerve-3me).

An action log (``core/action_log.py``) records every device-tool call of a run.
Replay turns it back into steps and runs them again:

- :func:`plan_steps` (pure) keeps the successful, state-changing actions;
- :func:`find_element` (pure) re-finds a tap's recorded ``target`` on the
  current screen, so a replay survives layout shifts;
- :func:`replay` runs the steps, waiting for each located element to appear,
  and reports whether the steps ran — ``completed`` or ``diverged`` at a step.

Whether the original BUG reproduced is not nerve's call — the caller's oracle
decides that; nerve only reports whether the steps ran.

Seams: ``_clock`` / ``_sleep`` are module-level so the tool layer can be tested
without real waiting; :func:`replay` also takes ``clock`` / ``sleep`` and a
``dispatch`` callable (default: direct controller calls).
"""

from __future__ import annotations

import inspect
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

import structlog

from linc_nerve.core import action_log

log = structlog.get_logger().bind(domain="replay")

_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep

MAX_GAP_S = 3.0
# Overall wall-clock budget for one replay. It sits inside the MCP call timeout
# (90 s) so a replay never keeps tapping/typing after its caller gave up and
# released the device.
MAX_RUN_S = 75.0
LOCATOR_KEYS: tuple[str, ...] = ("resource_id", "text", "content_desc", "class_name")

# Input limits for a replay request (the tool layer enforces them; tests patch them).
MAX_STEP_TIMEOUT_S = 120.0
MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_STEPS = 10_000

# secrets_env may only name variables in this namespace, so a replay cannot be
# used to type (and then read back) arbitrary daemon environment like GH_TOKEN.
_SECRET_VAR = re.compile(r"NERVE_SECRET_[A-Z0-9_]+")

# Locator match tiers, most specific first; a tier applies only when every key
# is set in the locator and wins only when exactly one element matches it.
_MATCH_TIERS: tuple[tuple[str, ...], ...] = (
    ("resource_id", "text"),
    ("resource_id",),
    ("text",),
    ("content_desc",),
)

Dispatch = Callable[[str, dict[str, Any]], Any]


@dataclass(frozen=True)
class Step:
    """One replayable action: the tool, its recorded args, and a tap's locator."""

    tool: str
    args: dict[str, Any]
    locator: dict[str, str] | None = None
    seq: int | None = None
    t_session_s: float | None = None
    duration_s: float = 0.0


class ReplayResult(TypedDict):
    outcome: str  # "completed" | "diverged"
    steps_total: int
    steps_done: int
    diverged_at: int | None
    reason: str | None
    duration_s: float


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _is_real(value: Any) -> bool:
    """A finite int/float — bools, strings, NaN and infinities are not."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_optional_real(value: Any) -> bool:
    return value is None or _is_real(value)


def _is_optional_int(value: Any) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def valid_step_timeout(value: Any) -> bool:
    """True for a finite step timeout in (0, MAX_STEP_TIMEOUT_S]."""
    return _is_real(value) and 0 < value <= MAX_STEP_TIMEOUT_S


# Field shape a replayable line must have: (field, check, what it must be).
_LINE_FIELDS: tuple[tuple[str, Callable[[Any], bool], str], ...] = (
    ("args", lambda value: isinstance(value, dict), "a JSON object"),
    ("seq", _is_optional_int, "an integer or null"),
    ("duration_ms", _is_optional_real, "a finite number or null"),
    ("t_session_s", _is_optional_real, "a finite number or null"),
)


def _line_problem(entry: dict[str, Any]) -> str | None:
    """Why a replayable line is malformed, or None when it is well-formed."""
    for field, check, wanted in _LINE_FIELDS:
        if not check(entry.get(field)):
            return f"{field} must be {wanted}"
    return None


def _is_replayable(entry: Any) -> bool:
    """A successful, non-readonly action line of one of the REPLAYABLE_TOOLS."""
    if not isinstance(entry, dict) or entry.get("type") != "action":
        return False
    tool = entry.get("tool")
    if not isinstance(tool, str) or tool not in REPLAYABLE_TOOLS:
        return False
    return entry.get("ok") is True and entry.get("readonly") is not True


def _locator_of(entry: dict[str, Any]) -> dict[str, str] | None:
    target = entry.get("target")
    if entry["tool"] not in action_log.TARGET_TOOLS or not isinstance(target, dict):
        return None
    return {key: str(target.get(key) or "") for key in LOCATOR_KEYS}


def _to_step(entry: dict[str, Any]) -> Step:
    return Step(
        tool=entry["tool"],
        args=dict(entry["args"]),
        locator=_locator_of(entry),
        seq=entry.get("seq"),
        t_session_s=entry.get("t_session_s"),
        duration_s=float(entry.get("duration_ms") or 0.0) / 1000,
    )


def plan_lines(lines: Iterable[tuple[int, Any]]) -> list[Step]:
    """The replayable steps of numbered action-log lines, in order.

    Keeps successful (``ok``), non-readonly ``action`` lines of the
    :data:`REPLAYABLE_TOOLS` (tap, long press, swipe, type, key, launch); drops
    the header, guard and guard_updated lines, failed actions and every other
    tool. Taps and long
    presses carry their recorded ``target`` as a locator. A kept line whose
    ``args`` is not an object, ``seq`` not an integer, or ``duration_ms`` /
    ``t_session_s`` not a finite number raises ValueError naming its line.
    """
    steps: list[Step] = []
    for number, entry in lines:
        if not _is_replayable(entry):
            continue
        problem = _line_problem(entry)
        if problem is not None:
            raise ValueError(f"action log line {number}: {problem}")
        steps.append(_to_step(entry))
    return steps


def plan_steps(entries: Iterable[Any]) -> list[Step]:
    """:func:`plan_lines` over *entries* numbered from 1."""
    return plan_lines(enumerate(entries, start=1))


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _unique(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    return candidates[0] if len(candidates) == 1 else None


def find_element(elements: Iterable[Any], locator: Mapping[str, str]) -> dict[str, Any] | None:
    """The element *locator* names on this screen, or None if absent or ambiguous.

    Tiers, first unique match wins: resource_id+text, resource_id, text,
    content_desc. ``class_name`` alone never locates (too many widgets share it).
    """
    pool = [(el, action_log._target_fields(el)) for el in action_log._iter_elements(elements)]
    for keys in _MATCH_TIERS:
        if not all(locator.get(key) for key in keys):
            continue
        hits = [el for el, f in pool if all(f[key] == locator[key] for key in keys)]
        match = _unique(hits)
        if match is not None:
            return match
    return None


def element_centre(element: Mapping[str, Any]) -> tuple[int, int] | None:
    """Centre of an element's bounds, or None when the bounds don't parse."""
    rect = action_log.parse_bounds(str(element.get("bounds", "")))
    if rect is None:
        return None
    x1, y1, x2, y2 = rect
    return (x1 + x2) // 2, (y1 + y2) // 2


def describe_locator(locator: Mapping[str, str]) -> str:
    """``text='Next' resource_id='…'`` — the set locator fields, for reasons."""
    return " ".join(f"{key}={locator[key]!r}" for key in LOCATOR_KEYS if locator.get(key))


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def _secret_pair(pair: str, environ: Mapping[str, str]) -> tuple[str, str | None]:
    seq, sep, var = (part.strip() for part in pair.partition("="))
    if not sep or not seq.isdigit() or not var:
        raise ValueError(f"secrets_env entries must be seq=ENV_VAR, got {pair.strip()!r}")
    if not _SECRET_VAR.fullmatch(var):
        raise ValueError(
            f"secrets_env variable {var!r} is not allowed: only NERVE_SECRET_[A-Z0-9_]+ "
            "variables can supply secrets"
        )
    return seq, environ.get(var)


def parse_secrets_env(spec: str, environ: Mapping[str, str]) -> dict[str, str]:
    """``"4=NERVE_SECRET_PW,7=NERVE_SECRET_OTP"`` → ``{"4": <value>, ...}`` from *environ*.

    Only ``NERVE_SECRET_*`` variables may be named. An unset variable is left
    out (that step later diverges with "missing secret"); a malformed pair or
    any other variable name raises ValueError. Values are never logged.
    """
    pairs = (_secret_pair(pair, environ) for pair in spec.split(",") if pair.strip())
    return {seq: value for seq, value in pairs if value is not None}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_CONTROLLER_CALLS: dict[str, Callable[[Any, str, dict[str, Any]], Any]] = {
    "nerve_tap": lambda c, s, a: c.tap(s, int(a["x"]), int(a["y"])),
    "nerve_long_press": lambda c, s, a: c.long_press(
        s, int(a["x"]), int(a["y"]), int(a.get("duration_ms", 500))
    ),
    "nerve_swipe": lambda c, s, a: c.swipe(
        s, a["start_x"], a["start_y"], a["end_x"], a["end_y"], a.get("duration_ms", 300)
    ),
    "nerve_type": lambda c, s, a: c.type_text(s, a["text"]),
    "nerve_key": lambda c, s, a: c.press_key(s, a["key"]),
    "nerve_launch": lambda c, s, a: c.launch(
        s, a["package"], uri=a.get("uri", ""), extras=a.get("extras")
    ),
}

# The only tools a replay ever runs (bead linc-nerve-3me): input and launch.
# App/device management (install, uninstall, clear data, permissions, emulator,
# reservations) and session control are never replayed.
REPLAYABLE_TOOLS: frozenset[str] = frozenset(_CONTROLLER_CALLS)


def controller_dispatch(controller: Any, serial: str) -> Dispatch:
    """Run steps straight on *controller* — no device-tool wrapper, no logging.

    The tool layer passes a dispatch through the registered device tools
    instead, so wrapper hooks (action log, guard) apply to replayed actions.
    """

    def dispatch(tool: str, args: dict[str, Any]) -> Any:
        call = _CONTROLLER_CALLS.get(tool)
        if call is None:
            return {"error": f"replay does not support {tool}"}
        call(controller, serial, args)
        return {"success": True}

    return dispatch


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class _Run:
    controller: Any
    serial: str
    step_timeout_s: float
    poll_s: float
    secrets: Mapping[str, str]
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    deadline: float = math.inf
    budget_s: float = math.inf

    def out_of_budget(self, ahead_s: float = 0.0) -> bool:
        """True once ``ahead_s`` more seconds would reach the run deadline."""
        return self.clock() + ahead_s >= self.deadline

    def budget_reason(self) -> str:
        return f"replay exceeded its {self.budget_s:g} s budget"


def _fresh_elements(run: _Run) -> list[Any]:
    """A cache-bypassing element read; [] on any failure (counts as not found)."""
    reader = run.controller.elements
    try:
        if "force_refresh" in inspect.signature(reader).parameters:
            return list(reader(run.serial, force_refresh=True))
        return list(reader(run.serial))
    except Exception as exc:
        log.debug("replay_elements_failed", serial=run.serial, error=str(exc))
        return []


def _wait_for(run: _Run, locator: Mapping[str, str]) -> tuple[int, int] | None:
    """Poll until *locator* matches an element with bounds; its centre, or None."""
    deadline = min(run.clock() + run.step_timeout_s, run.deadline)
    while True:
        match = find_element(_fresh_elements(run), locator)
        centre = element_centre(match) if match is not None else None
        if centre is not None:
            return centre
        now = run.clock()
        if now >= deadline:
            return None
        run.sleep(min(run.poll_s, deadline - now))


def _located_args(run: _Run, step: Step) -> tuple[dict[str, Any], str | None]:
    """Args with x/y moved to the located element's centre, or a divergence reason."""
    locator = step.locator or {}
    if not any(locator.get(key) for key in ("resource_id", "text", "content_desc")):
        return dict(step.args), None
    centre = _wait_for(run, locator)
    if centre is not None:
        return {**step.args, "x": centre[0], "y": centre[1]}, None
    if run.out_of_budget():
        return {}, f"{run.budget_reason()} waiting for {describe_locator(locator)}"
    if locator.get("resource_id") or locator.get("text"):
        found = f"element not found within {run.step_timeout_s}s"
        return {}, f"{found}: {describe_locator(locator)}"
    return dict(step.args), None


def _secret_args(run: _Run, step: Step) -> tuple[dict[str, Any], str | None]:
    secret = run.secrets.get(str(step.seq))
    if secret is None:
        return {}, f"missing secret for step seq {step.seq}"
    # "secret" is the boolean redaction flag, not a credential (bandit B105).
    return {"text": secret, "secret": True}, None  # nosec B105


def _is_secret_type(step: Step) -> bool:
    """A type step whose real text is not in the log (redacted or flagged secret)."""
    if step.tool != "nerve_type":
        return False
    return bool(step.args.get("secret")) or step.args.get("text") == action_log.REDACTED


def _prepare_args(run: _Run, step: Step) -> tuple[dict[str, Any], str | None]:
    if _is_secret_type(step):
        return _secret_args(run, step)
    if step.tool in action_log.TARGET_TOOLS and step.locator is not None:
        return _located_args(run, step)
    return dict(step.args), None


def _dispatch_step(dispatch: Dispatch, tool: str, args: dict[str, Any]) -> str | None:
    """Run one step; a failure reason (secret text scrubbed), or None on success."""
    try:
        ok, error = action_log.outcome_of(dispatch(tool, args))
    except Exception as exc:
        ok, error = False, str(exc)
    if ok:
        return None
    return action_log._scrub(f"{tool} failed: {error}", args)


# Tools whose recorded ``duration_ms`` holds the touch on the device that long.
_GESTURE_TOOLS: frozenset[str] = frozenset({"nerve_long_press", "nerve_swipe"})


def _gesture_s(tool: str, args: Mapping[str, Any]) -> float:
    """Seconds a step's gesture holds the device (its ``duration_ms``), else 0.

    A gesture that would outlast the run budget is not dispatched: the device
    would keep pressing after the replay (and its MCP call) returned.
    """
    value = args.get("duration_ms") if tool in _GESTURE_TOOLS else None
    return value / 1000 if _is_real(value) else 0.0


def _run_step(run: _Run, dispatch: Dispatch, step: Step) -> str | None:
    args, reason = _prepare_args(run, step)
    if reason is None and run.out_of_budget(_gesture_s(step.tool, args)):
        reason = run.budget_reason()  # a slow wait, or a gesture past the deadline
    if reason is not None:
        return reason
    return _dispatch_step(dispatch, step.tool, args)


def _gap(prev: Step, step: Step) -> float:
    """Recorded idle time between two steps, clamped to [0, MAX_GAP_S]."""
    if prev.t_session_s is None or step.t_session_s is None:
        return 0.0
    idle = step.t_session_s - prev.t_session_s - prev.duration_s
    return max(0.0, min(MAX_GAP_S, idle))


def _gap_then_step(run: _Run, dispatch: Dispatch, steps: list[Step], index: int) -> str | None:
    """Wait the recorded gap, then run step *index*; a divergence reason or None.

    Nothing runs once the gap would reach the run deadline.
    """
    step = steps[index]
    gap = _gap(steps[index - 1], step) if index else 0.0
    if run.out_of_budget(gap):
        return run.budget_reason()
    if gap > 0:
        run.sleep(gap)
    return _run_step(run, dispatch, step)


def _result(total: int, done: int, reason: str | None, duration_s: float) -> ReplayResult:
    return {
        "outcome": "completed" if reason is None else "diverged",
        "steps_total": total,
        "steps_done": done,
        "diverged_at": None if reason is None else done,
        "reason": reason,
        "duration_s": round(duration_s, 3),
    }


def replay(
    controller: Any,
    serial: str,
    steps: list[Step],
    *,
    step_timeout_s: float = 10.0,
    poll_s: float = 0.5,
    secrets: Mapping[str, str] | None = None,
    run_budget_s: float = MAX_RUN_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    dispatch: Dispatch | None = None,
) -> ReplayResult:
    """Run *steps* on *serial*; stop at the first step that cannot run.

    Located taps / long presses poll ``controller.elements`` (cache bypassed)
    until :func:`find_element` matches, then act on the element's centre. A
    locator with text or resource_id that never matches within
    *step_timeout_s* diverges — no fallback to the recorded coordinates.
    Secret ``nerve_type`` steps take their text from ``secrets[str(seq)]``.
    Between steps the recorded idle gap is waited, capped at 3 s.
    The whole run is bounded by *run_budget_s* (default :data:`MAX_RUN_S`):
    a step whose gap would reach the deadline is not run, and element waits
    stop at it — the run diverges with "replay exceeded its N s budget".

    *dispatch* runs one ``(tool, args)``; default :func:`controller_dispatch`.
    """
    start = clock()
    run = _Run(controller, serial, step_timeout_s, poll_s, secrets or {}, clock, sleep)
    run.deadline, run.budget_s = start + run_budget_s, run_budget_s
    send = dispatch or controller_dispatch(controller, serial)
    for index, step in enumerate(steps):
        reason = _gap_then_step(run, send, steps, index)
        if reason is not None:
            log.info("replay_diverged", serial=serial, step=index, tool=step.tool, reason=reason)
            return _result(len(steps), index, reason, clock() - start)
    log.info("replay_completed", serial=serial, steps=len(steps))
    return _result(len(steps), len(steps), None, clock() - start)
