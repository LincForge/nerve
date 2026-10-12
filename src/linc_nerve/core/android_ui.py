"""Android UI hierarchy + device info — elements, foreground, screen, density, orientation, logcat.

Split from android.py (#53) as a mixin to keep both files under the hot-file LOC
ceiling. References to test-patched / android.py module-globals (``time``,
``logger``, ``ADB_PATH``, ``ET``, ``re``, ``ElementNode``, ``parse_screen_size``,
constants) are routed through the parent module (``_android.<name>``) so patches
on ``linc_nerve.core.android.*`` keep working after the move.
"""

from __future__ import annotations

import re

from defusedxml.common import DefusedXmlException

from . import android as _android
from .device import UiHierarchyUnavailableError

# Logcat tag validation (linc-nerve-4dv.1): tags are interpolated directly
# into `logcat -d -s {tag}` and passed to `adb logcat -s {tag}` argv.
# Allowlist: must start with an alphanumeric or underscore, then word chars,
# dots, or hyphens (matches daemon/http_router._FILTER_ALLOWLIST_RE).
# Anchored ``\A...\Z`` + ``fullmatch`` (security review 2026-10-02 H1): ``^...$``
# with ``re.match`` accepts a trailing newline.
_LOG_TAG_RE = re.compile(r"\A[A-Za-z0-9_][\w.\-]*\Z")


def _validate_log_tag(tag: str) -> None:
    """Reject logcat tag filters that contain invalid characters or shell metachars."""
    if not _LOG_TAG_RE.fullmatch(tag):
        raise ValueError(
            f"Invalid log tag {tag!r}: must match {_LOG_TAG_RE.pattern} "
            "(alphanumeric or underscore followed by word characters, dots, or hyphens)"
        )


# Known automation drivers that hold the device's single UiAutomation
# connection. While one is alive, any new `uiautomator dump` is SIGKILLed
# by the system ("Killed", rc 137). A grid run left dev.mobile.maestro
# alive on the bench phone and blinded every subsequent nerve session
# (reactor 8bba730a / 280bbee7; same family as 44d68b62). These exact
# process names are safe to force-stop — they are test drivers, never user
# apps. NOTE: com.google.android.apps.wearables.maestro.companion (Pixel
# Watch companion) is an unrelated Google app and must never match.
_UIAUTOMATION_HOLDER_PKGS = (
    "dev.mobile.maestro",  # mobile.dev maestro driver
    "com.github.uiautomator",  # uiautomator2 server
    "com.github.uiautomator.test",
    "io.appium.uiautomator2.server",
    "io.appium.uiautomator2.server.test",
)

# The `uiautomator dump` command's OWN on-device process, as distinct from
# the driver app packages above. When a client-side TimeoutError abandons a
# dump call, this process can be left running on the device and still
# holding the single UiAutomation connection — an immediate client retry is
# then a second concurrent client against a still-wedged connection,
# reproducing the exact Killed/rc-137 failure this file exists to heal
# (#119 finding 1). Killing it is always safe regardless of what else is
# running: it can only ever be OUR own abandoned invocation, never another
# caller's app or driver.
#
# Matched by ARGV, not `comm` (round-1 review #119 finding 1): on Android,
# `/system/bin/uiautomator` is a shell wrapper that execs `app_process`, so
# the live dump process's `comm` (what `ps -A -o NAME` reports) is normally
# `app_process`/`app_process64`, NOT the literal string `uiautomator` — a
# `comm`-based match would silently never fire against a real device. The
# argv marker below is specific enough that it can only ever match our own
# `uiautomator dump` invocation, never the driver packages above (preserves
# the no-collateral-damage constraint #119 finding 2 is about).
#
# Named "MARKER" rather than "TOKEN" (round-1 discipline CI, #119 finding 7):
# bandit's B105 hardcoded-password heuristic flags any module-level string
# constant whose name ends in a small wordlist that includes "token" —
# nothing here is a credential, so the fix is the name, not a `# nosec`
# suppression (a suppression would mask a REAL future B105 hit on the same
# line if this constant is ever repurposed).
_UIAUTOMATOR_DUMP_ARGV_MARKER = "com.android.commands.uiautomator"

# ps/pkill/force-stop are sub-second on-device ops (unlike a `uiautomator
# dump`, which genuinely needs the full dump timeout) — bounding them tight
# keeps the auxiliary recovery machinery from eating the same 15s budget as
# a real dump (#119 finding 2).
_RECOVERY_AUX_TIMEOUT_S = 5

# Hard wall-clock budget for the WHOLE `_recover_from_dump_hang` sequence
# (tier-1 orphan cleanup + retry, tier-2 orphan cleanup + holder force-stop
# + retry — up to 3 dumps and ~8 auxiliary shell calls). Checked between
# tiers so a device that is genuinely wedged escalates to the reboot
# message instead of silently accumulating shell-call latency past the
# caller's tool ceiling (#119 finding 2 — round-0 undercounted the worst
# case; each individual `_shell` call can itself retry/backoff internally,
# so this bound is a backstop, not a precise guarantee, on top of the
# tighter aux timeout above).
_RECOVERY_DEADLINE_S = 45.0

# A dump can end with `</hierarchy>` (so `_dump_hierarchy` returns it) yet
# still fail `ET.fromstring` — observed 12x in 73s on the Pixel Watch 2 under
# load (reactor: 2026-07-14, role: watch bench). Retry the full dump a
# couple of times with a short backoff before giving up; never return []
# (reactor 8bba730a: silent `[]` on failure blinded agents).
_HIERARCHY_PARSE_MAX_ATTEMPTS = 3
_HIERARCHY_PARSE_RETRY_BACKOFF_S = 0.05

# Mirrors ``_shell``'s own default ``timeout`` param (android_reliability.py).
# Kept as an explicit constant here — rather than relying on ``_shell``'s
# default silently — so the recovery retry-dump calls can compute
# ``min(_DUMP_DEFAULT_TIMEOUT_S, remaining_budget)`` (CEO ITEM B / #119
# finding 2 round-2) instead of always passing the full default regardless
# of how little of ``_RECOVERY_DEADLINE_S`` is left.
_DUMP_DEFAULT_TIMEOUT_S = 15

# Errors the dump-hang recovery path must treat as "this shell call failed,
# handle gracefully" — NOT just the transient/retryable errors `_shell`
# itself retries on. A real wedge routinely trips the per-device circuit
# breaker (`_CIRCUIT_FAILURE_THRESHOLD = 3` in android.py) DURING the very
# first dump's own internal retries (each of its 3 attempts records a device
# failure), so by the time `_recover_from_dump_hang` runs, the circuit is
# already OPEN. Every recovery shell call then raises `_android.DeviceError`
# from `_check_circuit` — a DIFFERENT exception type than
# `_android._RETRYABLE_ERRORS`, which propagated UNCAUGHT out of
# `_kill_orphaned_dump_process` -> `_recover_from_dump_hang` -> callers
# before this fix, making the entire recovery path unreachable on a real
# wedge (round-2 review, CEO item A — BLOCKING). Recovery helpers must treat
# a circuit-open DeviceError the same as any other failed aux/retry call:
# best-effort, "nothing cleaned up" / "retry also failed" — never let it
# leak past `_recover_from_dump_hang`, which always converges on a
# `UiHierarchyUnavailableError` mentioning "reboot" regardless.
_RECOVERY_CAUGHT_ERRORS = (*_android._RETRYABLE_ERRORS, _android.DeviceError)


# Default element cap — keeps token budgets reasonable for AI consumers.
ELEMENT_NODE_CAP = 80


# `uiautomator dump` waits for the UI to go idle (a fixed on-device window,
# ~10 s, not configurable from the command line) and prints this when it
# never does — measured on the Galaxy Tab Active3 with Just Player paused on
# its controller overlay (linc-nerve-fv0; 10/10 runs, ~12.2 s raw). Not a
# UiAutomation holder conflict: no reboot remedy.
_IDLE_STATE_ERROR = "could not get idle state"


def _dump_failure_remedy(out: str, killed: list[str]) -> str:
    """The actionable remedy for a non-XML ``uiautomator dump`` output."""
    if _IDLE_STATE_ERROR in out:
        return (
            "the screen never went idle within uiautomator's fixed on-device idle "
            "wait (~10 s): some UI kept changing (e.g. a video player's on-screen "
            "controls or an animation) — dismiss or leave it and retry; not a "
            "UiAutomation holder conflict"
        )
    if killed:
        return (
            f"force-stopped leaked UiAutomation holder(s) {killed} but the dump "
            "still fails — reboot the device"
        )
    return (
        "another process may hold the device's single UiAutomation "
        "connection (leaked instrumentation/test driver); stop it or "
        "reboot the device"
    )


def _at_cap(out: list[dict], max_nodes: int | None) -> bool:
    """Whether *out* already holds *max_nodes* elements (None = no cap)."""
    return max_nodes is not None and len(out) >= max_nodes


class _UiMixin:
    """UI hierarchy dump + device-info query methods for AndroidController."""

    def _invalidate_elements_cache(self, serial: str) -> None:
        """Drop all cached UI hierarchies for this device.

        Called by any UI-mutating method (tap, swipe, long_press, type_text,
        press_key) so the next ``elements()`` reflects the post-mutation
        state. See inbox 23f41765.
        """
        for key in list(self._elements_cache):
            if key[0] == serial:
                del self._elements_cache[key]

    def elements(
        self,
        serial: str,
        max_depth: int = 30,
        force_refresh: bool = False,
        max_nodes: int | None = ELEMENT_NODE_CAP,
        quick_timeout_s: int | None = None,
    ) -> list[dict]:
        """Dump the UI hierarchy via UIAutomator.

        Returns a flat list of element dicts (compact, omitting falsy fields).
        At most ``max_nodes`` elements (default 80, the token-budget cap);
        ``None`` reads the whole tree (the guarded action-log session must
        see every label). Cached per ``(serial, max_depth, max_nodes)`` for
        ``_ELEMENTS_CACHE_TTL`` seconds
        to collapse repeated calls within a tight loop (inbox 23f41765).
        Pass ``force_refresh=True`` to bypass the cache. UI-mutating methods
        (tap, swipe, long_press, type_text, press_key) invalidate the cache
        for that serial automatically.
        Pass ``quick_timeout_s`` for a bounded single-attempt dump without
        hang recovery or UiAutomation holder cleanup (unguarded action log).
        Note: _shell() already acquires the device lock, so no extra lock here.

        Raises:
            UiHierarchyUnavailableError: the dump failed (timeout, killed by a
                conflicting UiAutomation holder, or unparseable output). An
                empty list is only ever returned for a genuinely empty screen
                (reactor 8bba730a: silent `[]` on failure blinded agents).
        """
        cache_key = (serial, max_depth, max_nodes)
        if not force_refresh:
            cached = self._elements_cache.get(cache_key)
            if cached is not None:
                ts, body = cached
                if _android.time.monotonic() - ts < _android._ELEMENTS_CACHE_TTL:
                    return body

        t0 = _android.time.monotonic()
        try:
            root = self._load_hierarchy_root(serial, quick_timeout_s)

            nodes: list[dict] = []
            self._walk_hierarchy(root, nodes, depth=0, max_depth=max_depth, max_nodes=max_nodes)
            self._record_metric("elements", serial, t0, success=True)
            self._elements_cache[cache_key] = (_android.time.monotonic(), nodes)
            return nodes
        except Exception as exc:
            self._record_metric("elements", serial, t0, success=False, error=exc)
            raise

    def _quick_dump(self, serial: str, timeout: int) -> str:
        """Run a single bounded ``uiautomator dump`` with no recovery or holder cleanup.

        Used for unguarded action-log tap annotation, where a stalled or
        broken UI hierarchy dump must not delay the user's action or
        force-stop competing UiAutomation holders.

        With ``timeout=3`` and internal ``_shell`` retries (``_MAX_RETRIES = 2``),
        the worst-case wall time is <= 10 s (3 attempts with 0.5s/1.0s backoff).

        Raises:
            UiHierarchyUnavailableError: On any shell failure, timeout, or
                non-XML output.
        """
        try:
            out = self._shell(serial, "uiautomator dump /dev/tty", timeout=timeout)
        except Exception as exc:
            raise UiHierarchyUnavailableError(
                f"uiautomator dump failed on {serial} (quick dump): {exc}"
            ) from exc

        end_tag = out.rfind("</hierarchy>")
        if end_tag > 0:
            return out[: end_tag + len("</hierarchy>")]

        detail = out.strip().splitlines()[0][:120] if out.strip() else "<empty output>"
        raise UiHierarchyUnavailableError(
            f"uiautomator dump on {serial} returned non-XML output: {detail!r}"
        )

    def _quick_hierarchy_root(self, serial: str, timeout: int) -> _android.ET.Element:
        """Fetch and parse hierarchy XML via the single bounded quick dump."""
        xml_str = self._quick_dump(serial, timeout)
        try:
            return _android.ET.fromstring(xml_str)
        except (_android.ET.ParseError, DefusedXmlException) as exc:
            excerpt = xml_str.strip()[:200] or "<empty output>"
            raise UiHierarchyUnavailableError(
                f"uiautomator dump on {serial} returned unparseable XML (output: {excerpt!r})"
            ) from exc

    def _load_hierarchy_root(
        self, serial: str, quick_timeout_s: int | None
    ) -> _android.ET.Element:
        """Fetch and parse hierarchy XML, routing quick reads around recovery."""
        if quick_timeout_s is None:
            xml_str = self._dump_hierarchy(serial)
            return self._parse_hierarchy_with_retry(serial, xml_str)
        return self._quick_hierarchy_root(serial, quick_timeout_s)

    def _parse_hierarchy_with_retry(self, serial: str, xml_str: str) -> _android.ET.Element:
        """Parse a uiautomator dump, retrying on malformed-but-terminated XML.

        ``xml_str`` is the already-fetched first dump (from ``_dump_hierarchy``
        in ``elements()``). If it fails to parse, re-runs the full dump
        (bypassing the elements cache — a fresh ``_dump_hierarchy`` call, not
        a cache read) up to ``_HIERARCHY_PARSE_MAX_ATTEMPTS`` times total,
        with a short backoff between attempts. Never returns an empty result
        on failure (reactor 8bba730a) — raises with a raw-output excerpt so
        the failure is diagnosable from the daemon log alone.
        """
        last_raw = xml_str
        for attempt in range(1, _HIERARCHY_PARSE_MAX_ATTEMPTS + 1):
            try:
                return _android.ET.fromstring(last_raw)
            except (_android.ET.ParseError, DefusedXmlException) as exc:
                if attempt == _HIERARCHY_PARSE_MAX_ATTEMPTS:
                    excerpt = last_raw.strip()[:200] or "<empty output>"
                    _android.logger.warning(
                        "hierarchy_parse_failed",
                        serial=serial,
                        attempt=attempt,
                        excerpt=excerpt,
                    )
                    raise UiHierarchyUnavailableError(
                        f"uiautomator dump on {serial} returned unparseable XML "
                        f"after {_HIERARCHY_PARSE_MAX_ATTEMPTS} attempts "
                        f"(output: {excerpt!r})"
                    ) from exc
                _android.logger.warning("hierarchy_parse_failed", serial=serial, attempt=attempt)
                _android.time.sleep(_HIERARCHY_PARSE_RETRY_BACKOFF_S)
                last_raw = self._dump_hierarchy(serial)
        raise AssertionError("unreachable")  # loop always returns or raises

    def _dump_hierarchy(self, serial: str) -> str:
        """Run ``uiautomator dump`` and return the trimmed hierarchy XML.

        Two distinct failure classes share one self-heal:
          - Non-XML output (typically ``Killed``, rc 137): the system
            SIGKILLs a second UiAutomation client while a leaked driver
            holds the connection — the dump *returns*, just not with
            hierarchy XML. Handled below (force-stop known holders, retry
            once).
          - A silent HANG (``_shell`` raises a retryable error, e.g.
            ``TimeoutError``): the dump never returns at all — same
            leaked-holder cause, but no output to route through the
            non-XML branch (reactor 9b9ba809, recurring PW2 post-t3
            wedge). Handled immediately below via ``_recover_from_dump_hang``,
            a two-tier, evidence-gated heal (#119 finding 1 + finding 2):
            first clean up our own orphaned dump process and retry with no
            force-stop (a merely slow device recovers here, untouched);
            only a SECOND failure — real evidence of a persistent holder
            conflict — escalates to force-stopping the known holder
            allowlist.

        Raises UiHierarchyUnavailableError when the dump stays broken after
        the self-heal so callers can distinguish "no dump" from "empty
        screen". A dedicated bench device's only remaining remedy at that
        point is a reboot — every terminal message here says so explicitly
        (auto-reboot is deliberately NOT performed in-call: this runs under
        a 90s tool ceiling and a reboot takes minutes).
        """
        try:
            out = self._shell(serial, "uiautomator dump /dev/tty")
        except _RECOVERY_CAUGHT_ERRORS as exc:
            return self._recover_from_dump_hang(serial, exc)

        end_tag = out.rfind("</hierarchy>")
        if end_tag > 0:
            return out[: end_tag + len("</hierarchy>")]

        # Non-XML output (e.g. "Killed"): a conflicting UiAutomation holder
        # is the common cause — recover once, then re-dump.
        killed = self._kill_uiautomation_holders(serial)
        if killed:
            _android.logger.warning(
                "uiautomation_holders_force_stopped", serial=serial, holders=killed
            )
            try:
                out = self._shell(serial, "uiautomator dump /dev/tty")
            except _RECOVERY_CAUGHT_ERRORS as exc:
                raise UiHierarchyUnavailableError(
                    f"uiautomator dump timed out on {serial} even after "
                    f"force-stopping leaked UiAutomation holder(s) {killed}: {exc}"
                ) from exc
            end_tag = out.rfind("</hierarchy>")
            if end_tag > 0:
                _android.logger.info("uiautomator_dump_recovered", serial=serial, holders=killed)
                return out[: end_tag + len("</hierarchy>")]

        detail = out.strip().splitlines()[0][:120] if out.strip() else "<empty output>"
        raise UiHierarchyUnavailableError(
            f"uiautomator dump failed on {serial} (output: {detail!r}); "
            f"{_dump_failure_remedy(out, killed)}"
        )

    def _recovery_dump_timeout(self, deadline: float) -> int | None:
        """Bound a recovery retry-dump call's timeout to the remaining budget.

        Returns ``None`` if the ``_RECOVERY_DEADLINE_S`` budget is already
        spent — the caller must not fire the dump at all (CEO ITEM B / #119
        finding 2, round 2: previously only the tier1->tier2 transition
        checked the deadline, so a wedge that ate the whole budget during
        tier-1's aux calls could still launch the tier-2 retry dump with a
        full, unscaled default timeout — the deadline was never re-checked
        immediately before firing). Otherwise returns
        ``min(_DUMP_DEFAULT_TIMEOUT_S, remaining)``, floored at 1s.

        NOT a precise wall-clock guarantee: ``_shell`` itself retries a
        failed attempt up to ``_MAX_RETRIES`` more times internally (with
        backoff), using the SAME passed-in ``timeout`` as each attempt's own
        hard timeout — so a single call can still overrun the returned
        value by up to roughly 3x in the worst case (see the caller's
        worst-case total, stated in the PR body). Scaling the timeout down
        as the budget shrinks, and refusing to fire once the budget is
        already gone, tightens the bound versus the prior behaviour (firing
        an unscaled default timeout, gated only between tiers) — it does
        not make the bound exact.
        """
        remaining = deadline - _android.time.monotonic()
        if remaining <= 0:
            return None
        return max(1, int(min(_DUMP_DEFAULT_TIMEOUT_S, remaining)))

    def _recover_from_dump_hang(self, serial: str, exc: BaseException) -> str:
        """Self-heal a silent dump HANG (``_shell`` raised a retryable error).

        Two-tier, evidence-gated heal (#119 findings 1 + 2). A bare single
        timeout is NOT, by itself, proof of a leaked UiAutomation holder —
        it could just be a slow device, or a driver another caller
        legitimately owns (e.g. a Maestro flow nightly BVT runs on this
        same shared bench). Force-stopping the holder allowlist on a bare
        timeout can therefore destroy a concurrent caller's in-flight
        session on a false positive. So:

          Tier 1 (always safe): kill our OWN orphaned dump process left
          behind by the timed-out attempt (never another caller's — see
          ``_kill_orphaned_dump_process``), then retry with NO force-stop
          of the holder allowlist. A merely slow device recovers here,
          untouched.

          Tier 2 (evidence-gated): only if the tier-1 retry ALSO fails
          (times out again, or returns non-XML output) do we have real
          corroborating evidence of a persistent conflict, not one bare
          timeout — NOW force-stop the known holder allowlist and retry a
          final time.

        Called only from the ``except`` branch of the FIRST dump attempt in
        ``_dump_hierarchy`` — never leaves the hang unhealed (reactor
        9b9ba809: the healing machinery previously existed 10 lines below
        this branch but was unreachable from it).

        Bounded by ``_RECOVERY_DEADLINE_S`` (#119 finding 2; tightened round
        2 / CEO ITEM B): the deadline is stamped once at entry and
        re-checked (i) inside the aux helpers, (ii) between tier 1 and
        tier 2, AND (iii) immediately before EACH retry dump call via
        ``_recovery_dump_timeout`` — which also scales that call's own
        timeout down to the remaining budget instead of always passing the
        full default. A genuinely wedged device escalates to the reboot
        message instead of silently chaining up to 3 dumps and ~8 auxiliary
        shell calls past the caller's tool ceiling. This is a tightened
        backstop, not an exact real-time guarantee: ``_shell`` can still
        internally retry a single call up to ~3x past the timeout it was
        given (see ``_recovery_dump_timeout``'s docstring and the PR body
        for the stated worst-case total).
        """
        _android.logger.warning("uiautomator_dump_timeout", serial=serial)
        deadline = _android.time.monotonic() + _RECOVERY_DEADLINE_S

        # Tier 1.
        orphan_killed = self._kill_orphaned_dump_process(serial, deadline=deadline)
        tier1_failed = True
        out = ""
        tier1_timeout = self._recovery_dump_timeout(deadline)
        if tier1_timeout is not None:
            try:
                out = self._shell(serial, "uiautomator dump /dev/tty", timeout=tier1_timeout)
                tier1_failed = out.rfind("</hierarchy>") <= 0
            except _RECOVERY_CAUGHT_ERRORS:
                tier1_failed = True

        if not tier1_failed:
            end_tag = out.rfind("</hierarchy>")
            _android.logger.info(
                "uiautomator_dump_recovered",
                serial=serial,
                holders=[],
                orphan_killed=orphan_killed,
            )
            return out[: end_tag + len("</hierarchy>")]

        if _android.time.monotonic() >= deadline:
            # Recovery's own time budget is spent — do not chain into tier
            # 2 (a second orphan sweep + the full holder allowlist +
            # another dump). Escalate now rather than risk blowing the
            # caller's tool ceiling.
            raise UiHierarchyUnavailableError(
                f"uiautomator dump timed out on {serial}: {exc}; recovery time budget "
                f"({_RECOVERY_DEADLINE_S:.0f}s) was exhausted after tier-1 cleanup before "
                "evidence-gated force-stop could run — reboot the device"
            )

        # Tier 2 — the plain retry also failed: real evidence of a
        # persistent holder conflict, not a bare single timeout. Clean up
        # any fresh orphan the tier-1 retry itself may have left, THEN
        # force-stop the known allowlist and retry a final time.
        tier2_orphan_killed = self._kill_orphaned_dump_process(serial, deadline=deadline)
        orphan_killed = orphan_killed or tier2_orphan_killed
        killed = self._kill_uiautomation_holders(
            serial, deadline=deadline, timeout=_RECOVERY_AUX_TIMEOUT_S
        )
        if killed:
            _android.logger.warning(
                "uiautomation_holders_force_stopped", serial=serial, holders=killed
            )
        orphan_note = (
            " an orphaned dump process was also found and killed;" if orphan_killed else ""
        )

        tier2_timeout = self._recovery_dump_timeout(deadline)
        if tier2_timeout is None:
            # Budget spent during tier-2's own orphan sweep + holder
            # force-stop — do not fire the evidence-gated retry dump at
            # all (CEO ITEM B round 2: this checkpoint didn't exist before;
            # the retry always fired with the full default timeout
            # regardless of how much budget tier-2 cleanup had already
            # consumed).
            raise UiHierarchyUnavailableError(
                f"uiautomator dump timed out on {serial}: {exc};{orphan_note} recovery time "
                f"budget ({_RECOVERY_DEADLINE_S:.0f}s) was exhausted during tier-2 cleanup "
                "before the evidence-gated retry dump could run — reboot the device"
            )

        try:
            out = self._shell(serial, "uiautomator dump /dev/tty", timeout=tier2_timeout)
        except _RECOVERY_CAUGHT_ERRORS as retry_exc:
            remedy = (
                f"force-stopped leaked UiAutomation holder(s) {killed} but the "
                "retry dump also timed out — reboot the device"
                if killed
                else "no known UiAutomation holder was running to force-stop; reboot the device"
            )
            raise UiHierarchyUnavailableError(
                f"uiautomator dump timed out on {serial}: {exc};{orphan_note} {remedy}"
            ) from retry_exc

        end_tag = out.rfind("</hierarchy>")
        if end_tag > 0:
            _android.logger.info(
                "uiautomator_dump_recovered",
                serial=serial,
                holders=killed,
                orphan_killed=orphan_killed,
            )
            return out[: end_tag + len("</hierarchy>")]

        # Retry didn't raise but also didn't return hierarchy XML (e.g.
        # "Killed" on the retry itself) — same terminal shape as the
        # non-XML branch in ``_dump_hierarchy``, with the original timeout
        # context preserved so the log/exception still names the hang.
        detail = out.strip().splitlines()[0][:120] if out.strip() else "<empty output>"
        remedy = (
            f"force-stopped leaked UiAutomation holder(s) {killed} but the dump "
            "still fails — reboot the device"
            if killed
            else "another process may hold the device's single UiAutomation "
            "connection (leaked instrumentation/test driver); stop it or "
            "reboot the device"
        )
        raise UiHierarchyUnavailableError(
            f"uiautomator dump timed out on {serial} then failed on retry "
            f"(output: {detail!r});{orphan_note} {remedy}"
        )

    def _kill_orphaned_dump_process(self, serial: str, deadline: float | None = None) -> bool:
        """Best-effort kill of an orphaned ``uiautomator dump`` process.

        When a client-side ``TimeoutError`` abandons a dump call, the
        on-device dump process it spawned can still be running, still
        holding the device's single UiAutomation connection — an
        immediate client retry is then a second concurrent client against
        a still-wedged connection (#119 finding 1: this is why the retry
        was previously plausibly a no-op).

        Matched by ARGV (``ps -A -o PID,ARGS``), not ``comm`` — see
        ``_UIAUTOMATOR_DUMP_ARGV_MARKER`` for why a ``comm``-based match
        (the round-0 implementation) likely never fired against a real
        device. Always safe to run unconditionally, unlike
        ``_kill_uiautomation_holders`` below: this only ever targets OUR
        OWN abandoned ``uiautomator dump`` invocation (matched by a
        specific argv token, never a package), so it can never destroy
        another caller's session. ``deadline`` (a ``time.monotonic()``
        timestamp, #119 finding 2) skips the check entirely once passed —
        best-effort recovery, not worth spending more of the caller's tool
        budget on. Best-effort throughout: shell errors here must not mask
        the original dump failure. Returns True if a process was found and
        killed.
        """
        if deadline is not None and _android.time.monotonic() >= deadline:
            return False
        try:
            ps_out = self._shell(serial, "ps -A -o PID,ARGS", timeout=_RECOVERY_AUX_TIMEOUT_S)
        except _RECOVERY_CAUGHT_ERRORS:
            return False
        pid: str | None = None
        for line in ps_out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and _UIAUTOMATOR_DUMP_ARGV_MARKER in parts[1]:
                pid = parts[0]
                break
        if pid is None:
            return False
        if deadline is not None and _android.time.monotonic() >= deadline:
            return False
        try:
            self._shell(serial, f"kill {pid}", timeout=_RECOVERY_AUX_TIMEOUT_S)
        except _RECOVERY_CAUGHT_ERRORS:
            return False
        return True

    def _kill_uiautomation_holders(
        self,
        serial: str,
        deadline: float | None = None,
        timeout: int | None = None,
    ) -> list[str]:
        """Force-stop known leaked automation drivers; return what was stopped.

        Matches exact process names against the ``_UIAUTOMATION_HOLDER_PKGS``
        allowlist only — never kills arbitrary processes. Best-effort: shell
        errors here must not mask the original dump failure. ``deadline`` (a
        ``time.monotonic()`` timestamp, #119 finding 2) stops issuing further
        force-stop calls once passed, returning whatever was stopped so far.
        ``timeout``, if given, bounds each auxiliary ``ps``/``am force-stop``
        call tighter than the ``_shell`` default (#119 finding 2 — only
        ``_recover_from_dump_hang`` passes this; other callers, e.g. the
        ``_dump_hierarchy`` Killed/rc-137 branch and ``ensure_ready``'s
        leak-heal path, keep the existing default and are unaffected).

        Destructive to whatever currently holds one of these packages,
        including a legitimate concurrent caller's driver — callers of this
        method MUST have real evidence of a persistent conflict first (a
        bare single timeout is not evidence; see ``_recover_from_dump_hang``
        tier 2, #119 finding 2), never call it on a bare first timeout.
        """
        if deadline is not None and _android.time.monotonic() >= deadline:
            return []
        shell_kwargs = {"timeout": timeout} if timeout is not None else {}
        try:
            ps_out = self._shell(serial, "ps -A -o NAME", **shell_kwargs)
        except _RECOVERY_CAUGHT_ERRORS:
            return []
        running = {line.strip() for line in ps_out.splitlines()}
        stopped: list[str] = []
        for pkg in _UIAUTOMATION_HOLDER_PKGS:
            if deadline is not None and _android.time.monotonic() >= deadline:
                break
            if pkg in running:
                try:
                    self._shell(serial, f"am force-stop {pkg}", **shell_kwargs)
                except _RECOVERY_CAUGHT_ERRORS:
                    continue
                stopped.append(pkg)
        return stopped

    def _walk_hierarchy(
        self,
        element: _android.ET.Element,
        out: list[dict],
        depth: int,
        max_depth: int,
        max_nodes: int | None = ELEMENT_NODE_CAP,
    ) -> None:
        """Recursively walk the XML hierarchy into flat element list."""
        if depth > max_depth or _at_cap(out, max_nodes):
            return

        node = _android.ElementNode(
            index=len(out),
            resource_id=element.get("resource-id", ""),
            text=element.get("text", ""),
            content_desc=element.get("content-desc", ""),
            class_name=element.get("class", ""),
            bounds=element.get("bounds", ""),
            clickable=element.get("clickable") == "true",
            enabled=element.get("enabled") != "false",
            checked=element.get("checked") == "true",
            focused=element.get("focused") == "true",
            scrollable=element.get("scrollable") == "true",
        )

        # Only include nodes with meaningful content
        if node.text or node.content_desc or node.resource_id or node.clickable or node.scrollable:
            out.append(node.to_dict())

        for child in element:
            self._walk_hierarchy(child, out, depth + 1, max_depth, max_nodes)

    def get_foreground(self, serial: str) -> dict[str, str]:
        """Get the foreground activity.

        Parses the full ``dumpsys activity activities`` output rather than
        pre-filtering on the device. Android 14+ emits ``ResumedActivity:``
        (no ``m`` prefix); pre-14 emits ``mResumedActivity:``. Filtering
        server-side with ``grep mResumedActivity`` drops Android 14+ lines.
        See improvement 977b7180.
        """
        output = self._shell(serial, "dumpsys activity activities")
        # Match: (m)ResumedActivity: ActivityRecord{<hash> u<N> com.pkg/.Activity t<N>...}
        match = _android.re.search(
            r"\b(?:m)?ResumedActivity:\s*ActivityRecord\{[^}]*?\s(\S+?)/(\S+?)[\s,}]",
            output,
        )
        if match:
            return {"package": match.group(1), "activity": match.group(2)}
        return {"package": "", "activity": ""}

    def get_screen_size(self, serial: str) -> dict[str, int]:
        """Get effective screen dimensions accounting for current orientation.

        ``wm size`` returns the device's native (portrait) dimensions even
        when the device is in landscape. When orientation is landscape the
        width/height are swapped to reflect the usable surface. See
        improvement 977b7180.
        """
        dims = _android.parse_screen_size(self._shell(serial, "wm size"))
        if dims is None:
            return {"width": 0, "height": 0}
        width, height = dims
        orientation = self.get_orientation(serial)
        if orientation.startswith("landscape"):
            width, height = height, width
        return {"width": width, "height": height}

    def get_density(self, serial: str) -> dict[str, float]:
        """Get display density. ``wm density`` → 'Physical density: <dpi>'.

        ``density_factor = dpi / 160`` (Android's mdpi baseline) is the dp→px
        multiplier used by callers (e.g. crucible BVT ``assert_min_size``).
        Returns zeros if the value can't be read so callers can hard-fail.
        """
        output = self._shell(serial, "wm density")
        match = _android.re.search(r"density:\s*(\d+)", output)
        if not match:
            return {"density_dpi": 0, "density_factor": 0.0}
        dpi = int(match.group(1))
        return {"density_dpi": dpi, "density_factor": dpi / 160.0}

    def get_orientation(self, serial: str) -> str:
        """Get screen orientation.

        Tries three signals in the full ``dumpsys input`` output:
        1. ``SurfaceOrientation: N`` — pre-Android 15 format
        2. ``Viewport INTERNAL: ... orientation=N`` — Android 15 OEM format
        3. ``InputDeviceOrientation: Rotation{0,90,180,270}`` — fallback
        See improvement 977b7180.
        """
        output = self._shell(serial, "dumpsys input")
        rotation_map = {
            0: "portrait",
            1: "landscape",
            2: "portrait_reverse",
            3: "landscape_reverse",
        }
        match = _android.re.search(r"SurfaceOrientation:\s*(\d)", output)
        if match:
            return rotation_map.get(int(match.group(1)), "portrait")
        match = _android.re.search(r"Viewport\s+INTERNAL:.*?\borientation=(\d)", output)
        if match:
            return rotation_map.get(int(match.group(1)), "portrait")
        match = _android.re.search(r"InputDeviceOrientation:\s*Rotation(\d+)", output)
        if match:
            degree_map = {
                0: "portrait",
                90: "landscape",
                180: "portrait_reverse",
                270: "landscape_reverse",
            }
            return degree_map.get(int(match.group(1)), "portrait")
        return "portrait"

    def logcat(self, serial: str, lines: int = 50, tag: str = "") -> str:
        """Get recent logcat lines, optionally filtered by tag.

        Android's ``logcat -t N -s TAG`` reads the last N raw buffer lines
        first and *then* applies the tag filter, so rare-tag events get
        dropped. When a tag is set we omit ``-t`` and trim to N matching
        lines in Python. See improvement 40d8edf6.
        """
        if tag:
            _validate_log_tag(tag)
            output = self._shell(serial, f"logcat -d -s {_android.shlex.quote(tag)}")
            result_lines = output.splitlines()
            return "\n".join(result_lines[-lines:])
        return self._shell(serial, f"logcat -d -t {int(lines)}")

    def get_log_stream_cmd(self, serial: str, tag: str = "") -> list[str]:
        """Get the subprocess command list to stream logcat continuously."""
        cmd = [_android.ADB_PATH, "-s", serial, "logcat", "-v", "threadtime"]
        if tag:
            _validate_log_tag(tag)
            cmd.extend(["-s", tag])
        return cmd
