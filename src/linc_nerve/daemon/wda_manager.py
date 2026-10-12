"""WDA lifecycle management for the nerve daemon.

Auto-starts WebDriverAgent for the first booted iOS simulator or an
explicitly allowlisted physical device. Restarts on crash. Tracks port per
device.

Attach only, never boot
------------------------
The device-poll candidate list (``ios_devices``) can be minutes stale, so
immediately before calling ``_start_wda`` for a simulator candidate,
``reconcile`` re-reads its LIVE state through the injectable
``sim_state(udid) -> str`` seam (default: parse
``xcrun simctl list devices --json``) and proceeds only when that state is
exactly ``"Booted"``. A non-Booted candidate is *skipped*, not blacklisted
(``_failed_serials`` does not gain the serial) — it is naturally retried on
the next poll once something else (the nightly, a human) boots it.
**Auto-start must never boot a simulator itself.**

Physical devices are opt-in only, via the ``NERVE_WDA_AUTOSTART_PHYSICAL``
environment variable — a comma-separated allowlist of UDIDs; unset or empty
means none are auto-started. WDA on a physical iPhone is user-visible (it
launches an app on the device), so a stray auto-start is a much bigger
surprise than it is for a simulator. A non-allowlisted physical UDID is
skipped the same way a not-yet-booted simulator is: not added to
``_failed_serials``.

``_start_wda`` also refuses, before touching simctl/xcodebuild at all, when
the prebuilt WDA products (the ``.xctestrun`` under the relevant build
products directory) are missing — the daemon poll loop must never trigger a
from-scratch ``xcodebuild`` build. Unlike the two skips above, this one DOES
add the serial to ``_failed_serials`` (a missing build is a structural
problem that will not resolve itself before the underlying tooling changes).

Finally, as defence in depth against a race between the ``reconcile``-time
Booted re-check and the actual start attempt, ``_start_wda`` shuts a
simulator back down (via the injectable ``sim_shutdown(udid)`` seam) if a
failed start leaves it Booted when it was not Booted at the top of the
attempt.

One listing per tick, off the shared tool worker (linc-nerve#970638ee)
-------------------------------------------------------------------------
Two follow-up fixes after a nightly abort (2026-09-15) traced to this poll
loop running on the daemon's single shared tool-worker thread:

1. The live-state re-check above used to be a per-candidate ``sim_state(udid)``
   probe — one ``xcrun simctl list devices --json`` process per simulator
   candidate on every 30s tick. ``reconcile`` now takes ONE per-tick snapshot
   through the injectable ``sim_states() -> dict[str, str]`` seam (default:
   one ``simctl`` listing, parsed into ``{udid: state}``) and shares it across
   every candidate that needs a Booted decision this tick — skipped entirely
   when no simulator candidate needs one. The per-UDID ``sim_state`` seam is
   NOT dead: it is still the single fresh read ``_cleanup_stray_boot`` makes
   after a failed start, which by definition cannot be satisfied by a
   snapshot taken before the attempt.
2. ``_start_wda`` no longer calls ``WDAManager.get_or_start`` inline — a slow
   or hanging ``xcodebuild``/WDA launch used to block whatever thread called
   ``reconcile`` (the daemon's single shared tool-worker), starving every
   other tool call for the daemon. It now hands that call to a dedicated
   background thread (injectable ``start_runner``, default a daemon
   ``threading.Thread``) and returns immediately, recording an in-flight
   marker (``_starting_serial``). While a start is in flight, ``reconcile``
   starts nothing else and does not re-probe that serial — the single-active
   invariant extends to "at most one active-or-starting session at a time."
   The thread's own completion (under a lock) records success or failure
   exactly as the old inline call did.
"""
from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed argv, no shell, hard timeouts
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from linc_nerve.core.device import DeviceType
from linc_nerve.core.ios import _is_physical_udid

if TYPE_CHECKING:
    from linc_nerve.core.wda import WDAManager, WDASession

log = structlog.get_logger().bind(domain="wda_manager")

#: Comma-separated allowlist of physical-device UDIDs auto-start may attach
#: WDA to. Unset or empty means none — physical auto-start is opt-in only.
_AUTOSTART_PHYSICAL_ENV = "NERVE_WDA_AUTOSTART_PHYSICAL"


def _default_sim_states() -> dict[str, str]:
    """Parse ``xcrun simctl list devices --json`` ONCE for every simulator's
    live state (linc-nerve#970638ee).

    Shared by every simulator candidate that needs a Booted decision on a
    given ``reconcile`` tick — replaces issuing one ``simctl`` listing per
    candidate, which could mean ten-plus subprocess spawns on a single busy
    tick. Returns ``{}`` on any probe failure; callers look up a UDID via
    ``.get(udid, "Unknown")``, so a missing entry (absent UDID, or a failed
    probe that returns nothing at all) reads as "not Booted" either way —
    the same fail-safe posture the old per-UDID probe had.
    """
    try:
        result = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["xcrun", "simctl", "list", "devices", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        data = json.loads(result.stdout)
    except Exception as e:
        log.error("wda_sim_states_probe_failed", error=str(e))
        return {}

    states: dict[str, str] = {}
    for devices in data.get("devices", {}).values():
        for device in devices:
            udid = device.get("udid")
            if udid:
                states[udid] = device.get("state", "Unknown")
    return states


def _default_sim_state(udid: str) -> str:
    """Parse ``xcrun simctl list devices --json`` for one UDID's live state.

    Production only calls this for the single fresh post-failure read
    ``_cleanup_stray_boot`` makes after a failed start — every other live
    -state decision goes through the per-tick ``sim_states()`` snapshot
    above, which this delegates to rather than parsing the listing twice.
    Returns ``"Unknown"`` if the UDID isn't present in the listing or the
    probe itself fails — callers treat any non-``"Booted"`` state as "do
    not touch it".
    """
    return _default_sim_states().get(udid, "Unknown")


def _default_sim_shutdown(udid: str) -> None:
    """Shut down a simulator via ``xcrun simctl shutdown <udid>``."""
    try:
        subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["xcrun", "simctl", "shutdown", udid],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as e:
        log.error("wda_sim_shutdown_failed", udid=udid, error=str(e))


def _default_wda_products_available(is_physical: bool) -> bool:
    """Check for a cached ``.xctestrun`` under the right build products dir.

    Mirrors ``core.wda_build.build_wda``'s own cache check but never
    builds — the daemon poll loop must never kick off a from-scratch
    ``xcodebuild``.
    """
    from linc_nerve.core.wda import _WDA_BUILD_DIR
    from linc_nerve.core.wda_build import _WDA_DEVICE_BUILD_DIR, _find_xctestrun

    build_dir = _WDA_DEVICE_BUILD_DIR if is_physical else _WDA_BUILD_DIR
    return _find_xctestrun(build_dir) is not None


def _default_start_runner(target: Callable[[], None]) -> threading.Thread:
    """Spawn ``target`` on a dedicated daemon thread and return it, unstarted
    (linc-nerve#970638ee).

    ``_start_wda`` hands ``WDAManager.get_or_start`` to this seam instead of
    calling it inline, so a slow or hanging WDA build/launch never blocks
    whatever thread called ``reconcile`` — normally the daemon's single
    shared tool-worker. Daemon thread: an in-flight WDA start must never by
    itself keep the process alive.
    """
    return threading.Thread(target=target, daemon=True)


def _seam(explicit: Callable[..., Any] | None, default: Callable[..., Any]) -> Callable[..., Any]:
    """Resolve an injectable seam: the explicit override if given, else the
    default. A tiny free function so `__init__` stays straight-line (no
    per-parameter branching) as the seam count grows.
    """
    return explicit if explicit is not None else default


class DaemonWDAManager:
    """Manages WDA lifecycle in the daemon polling loop."""

    def __init__(
        self,
        sim_state: Callable[[str], str] | None = None,
        sim_states: Callable[[], dict[str, str]] | None = None,
        sim_shutdown: Callable[[str], None] | None = None,
        wda_products_available: Callable[[bool], bool] | None = None,
        start_runner: Callable[[Callable[[], None]], Any] | None = None,
    ) -> None:
        self._active_serial: str | None = None
        self._active_session: WDASession | None = None
        self._wda_port: int = 8100
        self._wda_manager: WDAManager | None = None
        # Serials for which a start attempt has already failed structurally
        # (missing API, unsupported platform, missing build tooling). Once a
        # serial lands here, reconcile stops re-attempting it every 30s;
        # pruned when the serial drops out of the current raw device list.
        self._failed_serials: set[str] = set()
        # Set the moment `_start_wda` hands a start to the background thread
        # and cleared only by that thread's own completion (linc-nerve#970638ee)
        # — `reconcile` treats a non-None value the same as an active session
        # for the single-active invariant: start nothing else, re-probe
        # nothing else, while it is set.
        self._starting_serial: str | None = None
        # Guards the handful of fields the background start thread and
        # `reconcile` (running on the daemon's shared tool-worker) both
        # touch: `_starting_serial`, `_active_serial`, `_active_session`,
        # `_failed_serials`.
        self._lock = threading.Lock()
        # Injectable seams (linc-nerve#970638ee) — real implementations shell
        # out to simctl / touch the filesystem, so tests supply fakes instead
        # of exercising the real device / build tree. `sim_state` (singular)
        # is the one-UDID live re-check `_cleanup_stray_boot` makes after a
        # failed start; `sim_states` (plural) is the once-per-tick snapshot
        # `reconcile` shares across every candidate that needs a decision.
        self._sim_state = _seam(sim_state, _default_sim_state)
        self._sim_states = _seam(sim_states, _default_sim_states)
        self._sim_shutdown = _seam(sim_shutdown, _default_sim_shutdown)
        self._wda_products_available = _seam(
            wda_products_available, _default_wda_products_available
        )
        self._start_runner = _seam(start_runner, _default_start_runner)

    @property
    def active_wda(self) -> str | None:
        return self._active_serial

    @property
    def wda_port(self) -> int:
        return self._wda_port

    def reconcile(self, ios_devices: list[Any]) -> None:
        """Reconcile WDA state with current iOS devices. Called every 30s.

        watchOS devices and devices that are not currently online are never
        selected as a WDA target — WDA does not run on watchOS, and an
        offline simulator can never pass health (linc-nerve#137).
        """
        # Drop give-up markers for serials no longer present in the RAW
        # device list (not just the filtered candidates), and do it before
        # any early return, so a device that disconnects and reconnects gets
        # a fresh attempt even when it was the only device on the bench
        # (linc-nerve#137 round-0 finding: pruning after the empty-candidates
        # return, or keying it on post-filter candidates, made the prune
        # unreachable in the single-device case).
        current_serials = {d.serial for d in ios_devices}
        self._failed_serials &= current_serials

        candidates = [
            d
            for d in ios_devices
            if getattr(d.device_type, "value", d.device_type) != DeviceType.WATCH.value
            and getattr(d, "state", "online") == "online"
        ]

        if not candidates:
            self._stop_wda()
            return

        if self._reconcile_active_wda(candidates):
            return

        if self._is_starting():
            # A start is already in flight on its own background thread
            # (linc-nerve#970638ee) — the single-active invariant extends to
            # it, so this tick starts nothing else and, notably, never calls
            # `sim_states()` to re-probe the in-flight serial or anyone else.
            return

        self._start_first_eligible(candidates)

    def _is_starting(self) -> bool:
        with self._lock:
            return self._starting_serial is not None

    def _start_first_eligible(self, candidates: list[Any]) -> None:
        """Start WDA for the first non-watchOS, online candidate that hasn't
        already failed structurally and is currently eligible for
        auto-start. Skip (don't abort) ineligible/blacklisted serials so one
        bad or not-yet-ready device never disables WDA for every other
        device on the bench (linc-nerve#137 round-0 finding).

        Split out of `reconcile` (linc-nerve#970638ee CRAP follow-up,
        mirroring the `_reconcile_active_wda` split): the eligibility gate
        added here is a distinct responsibility from `reconcile`'s own
        prune/filter/keep-active bookkeeping. Takes the per-tick `sim_states`
        snapshot exactly once here — not once per candidate — and shares it
        with `_autostart_eligible` and `_start_wda` (linc-nerve#970638ee).
        """
        sim_states = self._snapshot_sim_states(candidates)
        for device in candidates:
            if device.serial in self._failed_serials:
                continue
            is_physical = _is_physical_udid(device.serial)
            if not self._autostart_eligible(device.serial, is_physical, sim_states):
                continue
            self._start_wda(device.serial, is_physical=is_physical, sim_states=sim_states)
            break

    def _snapshot_sim_states(self, candidates: list[Any]) -> dict[str, str]:
        """Take the ONE per-tick `sim_states` listing, but only when at
        least one simulator candidate actually needs a Booted decision
        (linc-nerve#970638ee) — every candidate already blacklisted or
        physical has nothing to decide, so there is no reason to shell out
        to `simctl` on their behalf.
        """
        needs_snapshot = any(
            not _is_physical_udid(d.serial) and d.serial not in self._failed_serials
            for d in candidates
        )
        if not needs_snapshot:
            return {}
        return self._sim_states()

    def _autostart_eligible(
        self, serial: str, is_physical: bool, sim_states: dict[str, str]
    ) -> bool:
        """Gate a candidate immediately before it is handed to `_start_wda`.

        Physical devices require an explicit opt-in allowlist; simulators
        require a freshly re-checked live "Booted" state, read from this
        tick's shared `sim_states` snapshot (the poll-derived candidate list
        can be minutes stale). Neither skip touches `_failed_serials` — both
        are conditions that can resolve themselves without any code-side
        retry logic.
        """
        if is_physical:
            if not self._physical_autostart_allowed(serial):
                log.info("wda_autostart_skipped", serial=serial, reason="physical_not_allowlisted")
                return False
            return True

        state = sim_states.get(serial, "Unknown")
        if state != "Booted":
            log.info(
                "wda_autostart_skipped",
                serial=serial,
                reason="simulator_not_booted",
                state=state,
            )
            return False
        return True

    def _physical_autostart_allowed(self, serial: str) -> bool:
        """Read the `NERVE_WDA_AUTOSTART_PHYSICAL` opt-in allowlist."""
        allowlist = os.environ.get(_AUTOSTART_PHYSICAL_ENV, "")
        allowed = {udid.strip() for udid in allowlist.split(",") if udid.strip()}
        return serial in allowed

    def _reconcile_active_wda(self, candidates: list[Any]) -> bool:
        """Detect a crashed WDA session, then keep-or-stop against `candidates`.

        Separated from `reconcile` (linc-nerve#137 CRAP follow-up): this is
        the currently-active-session bookkeeping — a distinct responsibility
        from "pick the next candidate and start it" — and keeping it inline
        was inflating `reconcile`'s own complexity past what the new
        watchOS-filter/give-up-set logic already added.

        Returns True if reconcile should return early this poll (WDA is
        already running and its device is still present).
        """
        if self._active_session is not None and not self._active_session.is_running:
            log.warning("wda_crashed", serial=self._active_serial)
            self._active_session = None
            self._active_serial = None

        if self._active_serial:
            active_serials = {d.serial for d in candidates}
            if self._active_serial in active_serials:
                return True
            self._stop_wda()

        return False

    def _get_wda_manager(self) -> WDAManager:
        """Lazily construct (and cache) the real WDAManager.

        Cached on the instance rather than per-call: WDAManager() runs
        orphan-process cleanup / port-sweep side effects at construction
        time, which should happen once, not on every 30s reconcile.
        """
        if self._wda_manager is None:
            from linc_nerve.core.wda import WDAManager

            self._wda_manager = WDAManager()
        return self._wda_manager

    def _start_wda(
        self,
        serial: str,
        is_physical: bool = False,
        sim_states: dict[str, str] | None = None,
    ) -> None:
        """Start WDA for a device via the real WDAManager API.

        The cheap local checks (env var, prebuilt-products stat) stay on the
        calling thread; the potentially slow `WDAManager.get_or_start` call
        (an `xcodebuild`/WDA launch) is handed to `self._start_runner`
        (linc-nerve#970638ee) so it never blocks the caller — normally the
        daemon's single shared tool-worker, which `reconcile` also runs on.
        """
        wda_dir = os.environ.get("WDA_PROJECT_DIR", "")
        if not wda_dir:
            log.debug("wda_skipped_no_project_dir")
            return

        if not self._wda_products_available(is_physical):
            self._failed_serials.add(serial)
            log.info("wda_autostart_skipped", serial=serial, reason="wda_products_missing")
            return

        # Read from THIS tick's shared snapshot rather than a fresh probe —
        # `_start_wda` used to re-probe independently as defence in depth,
        # but that was exactly the per-candidate `simctl` call
        # linc-nerve#970638ee removes. `_cleanup_stray_boot`'s single
        # post-failure read (below) is the defence in depth now: it happens
        # after the attempt, so no snapshot taken before it could answer it.
        state_before = None if is_physical else (sim_states or {}).get(serial, "Unknown")

        manager = self._get_wda_manager()
        with self._lock:
            self._starting_serial = serial
        thread = self._start_runner(
            lambda: self._run_start(manager, serial, is_physical, state_before)
        )
        thread.start()

    def _run_start(
        self,
        manager: WDAManager,
        serial: str,
        is_physical: bool,
        state_before: str | None,
    ) -> None:
        """Body of the background start thread (linc-nerve#970638ee).

        Records success or failure under `self._lock` and always clears
        `_starting_serial` so `reconcile` picks back up on the next tick,
        whichever way this went.
        """
        try:
            session = manager.get_or_start(serial, is_physical=is_physical)
        except Exception as e:
            # Structural failure (unsupported platform, missing build
            # tooling, etc.) — give up on this serial rather than re-log
            # and re-attempt it on every future 30s poll.
            with self._lock:
                self._failed_serials.add(serial)
                self._starting_serial = None
            log.error("wda_start_failed", serial=serial, error=str(e))
            if not is_physical and state_before != "Booted":
                self._cleanup_stray_boot(serial, state_before)
            return

        with self._lock:
            self._active_serial = serial
            # Keep the handle: without it, nothing can ever detect a crash
            # or stop this session (linc-nerve#137 round-0 finding) — the
            # returned WDASession is what `_reconcile_active_wda` inspects
            # and what `_stop_wda` tells the WDAManager to tear down.
            self._active_session = session
            self._starting_serial = None
        log.info("wda_started", serial=serial, is_physical=is_physical)

    def _cleanup_stray_boot(self, serial: str, state_before: str | None) -> None:
        """Shut a simulator back down if a failed start left it Booted.

        Only reachable when the simulator was NOT Booted at the top of the
        `_start_wda` attempt (defence in depth — `reconcile`'s Booted
        re-check should make this unreachable in the normal poll path). If
        the failed start left it Booted anyway, auto-start must not leave
        it running: shut it back down via the injectable `sim_shutdown`
        seam rather than leaving a simulator it never should have touched
        booted.
        """
        state_after = self._sim_state(serial)
        if state_after == "Booted":
            self._sim_shutdown(serial)
            log.warning(
                "wda_autostart_cleanup",
                serial=serial,
                state_before=state_before,
                state_after=state_after,
            )

    def _stop_wda(self) -> None:
        """Stop the currently ACTIVE session, if any.

        Deliberately does not touch a session that is still `_starting_serial`
        (linc-nerve#970638ee): that start is running on its own background
        thread, `WDAManager.get_or_start` has no cancellation hook, and
        blocking here until it finishes would re-introduce the tool-worker
        stall this change exists to remove. Chosen trade-off: let the
        in-flight start run to completion on its own. If its device turns
        out to be gone by then, the NEXT `reconcile` tick's
        `_reconcile_active_wda` observes that (the serial isn't in
        `candidates`) and stops it normally — a start immediately followed
        by a stop, rather than a start interrupted mid-flight.

        Invariant: `_active_serial` is only ever set non-None in
        `_run_start`, which is only reachable once `_get_wda_manager` (via
        `_start_wda`) has already populated `_wda_manager` — so
        `_wda_manager` is always present here.
        """
        if self._active_serial is not None:
            self._wda_manager.stop(self._active_serial)  # type: ignore[union-attr]
        self._active_session = None
        self._active_serial = None

    def shutdown(self) -> None:
        self._stop_wda()
