"""Tests for the daemon's WDA lifecycle manager (linc-nerve#137).

Covers two original bugs in one function, plus five round-0 MINOS review
findings on the first fix:
1. ``_start_wda`` called a nonexistent ``IOSController.start_wda`` — every
   attempt raised ``AttributeError``, was swallowed, and retried forever.
2. ``reconcile`` selected ``ios_devices[0]`` with no platform filter, so a
   watchOS simulator could be chosen as a WDA target (WDA does not run on
   watchOS).
3. ``is_physical`` was computed from a ``DeviceType`` enum value that has no
   "physical" member, so it could never be True.
4. The candidate filter had no ``state`` check and a blacklisted head-of-list
   device disabled WDA for every other device on the bench.
5. The started ``WDASession`` was discarded, so nothing ever stopped it.
6. The crash-restart test hand-set a private attribute production never
   assigns, certifying a capability that did not exist.
7. The failed-serials prune was unreachable in the single-device case, so
   unplug/replug never recovered a failed serial.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import structlog.testing

from linc_nerve.daemon import wda_manager
from linc_nerve.daemon.wda_manager import DaemonWDAManager

# Simulator-shaped UDIDs (36 chars, 4 dashes) — `_is_physical_udid` treats
# these as simulators (`is_physical=False`), matching the intent of the
# tests that use them ("an iOS simulator phone").
SIM_PHONE_1 = "AAAAAAAA-1111-2222-3333-444444444401"
SIM_PHONE_A = "AAAAAAAA-1111-2222-3333-444444444402"
SIM_PHONE_B = "AAAAAAAA-1111-2222-3333-444444444403"
SIM_PHONE_ONLINE = "AAAAAAAA-1111-2222-3333-444444444404"
SIM_OFFLINE_1 = "AAAAAAAA-1111-2222-3333-444444444405"
SIM_OFFLINE_2 = "AAAAAAAA-1111-2222-3333-444444444406"
SIM_BAD = "AAAAAAAA-1111-2222-3333-444444444407"
SIM_GOOD = "AAAAAAAA-1111-2222-3333-444444444408"
SIM_PHONE_X = "AAAAAAAA-1111-2222-3333-444444444409"
SIM_NOT_BOOTED = "AAAAAAAA-1111-2222-3333-44444444440a"
SIM_STALE_CLEANUP = "AAAAAAAA-1111-2222-3333-44444444440b"
SIM_PRODUCTS_MISSING = "AAAAAAAA-1111-2222-3333-44444444440c"

# A real physical-device UDID shape (not 36 chars / 4 dashes) —
# `_is_physical_udid` treats this as a physical device (`is_physical=True`).
PHYSICAL_UDID = "00008110-000A4D3C0123456E"
PHYSICAL_UDID_DENIED = "00008110-000A4D3C0123457F"


def _mock_device(
    serial: str, device_type: str = "phone", state: str = "online"
) -> MagicMock:
    device = MagicMock()
    device.serial = serial
    device.device_type = MagicMock(value=device_type)
    device.state = state
    return device


def _running_session() -> MagicMock:
    """A WDASession stand-in that reports itself as running."""
    session = MagicMock()
    session.is_running = True
    return session


class _AlwaysBooted(dict):
    """``sim_states()`` stand-in that reports every UDID as "Booted".

    linc-nerve#970638ee replaced the per-candidate ``sim_state(udid)`` probe
    with a per-tick ``sim_states() -> dict[str, str]`` snapshot looked up via
    ``.get(serial, "Unknown")``. Most of the suite below doesn't care which
    UDID is being asked about — it just wants "yes, Booted" — so this
    overrides ``.get`` instead of requiring every test to pre-populate an
    entry per serial.
    """

    def get(self, key, default=None):
        return "Booted"


class _ImmediateThread:
    """Fake "thread" that runs its target synchronously in ``.start()``.

    linc-nerve#970638ee's real default ``start_runner`` spawns a background
    ``threading.Thread`` so a WDA start never blocks the caller — correct for
    production, but it would turn every synchronous assertion in this
    pre-existing suite ("reconcile started WDA, check `active_wda` now") into
    a race. Tests that specifically exercise the new async behavior construct
    their own ``DaemonWDAManager(start_runner=...)`` backed by a real thread
    instead of relying on this default.
    """

    def __init__(self, target) -> None:
        self._target = target

    def start(self) -> None:
        self._target()


def _synchronous_start_runner(target):
    return _ImmediateThread(target)


@pytest.fixture(autouse=True)
def _default_autostart_seams(monkeypatch):
    """Default every test to "sim is Booted", "WDA products are present",
    and a start that completes synchronously.

    linc-nerve#970638ee added a live re-check of simulator boot state
    (``sim_states``) and a prebuilt-products check (``wda_products_available``)
    ahead of every real start attempt, plus a background ``start_runner`` seam
    so a WDA start never blocks the caller. The pre-existing suite below
    constructs ``DaemonWDAManager()`` with no seams supplied, so without this
    default the real seams would shell out to ``xcrun simctl``, stat the real
    ``~/.linc/wda/build*`` tree, and race a real background thread — exactly
    what the injectable-seam rule exists to prevent. Tests exercising the new
    not-Booted / products-missing / physical-allowlist / in-flight-start
    paths override the manager's own seams (or the env var) directly instead
    of relying on this default.
    """
    monkeypatch.setattr(wda_manager, "_default_sim_state", lambda udid: "Booted")
    monkeypatch.setattr(wda_manager, "_default_sim_states", lambda: _AlwaysBooted())
    monkeypatch.setattr(wda_manager, "_default_wda_products_available", lambda is_physical: True)
    monkeypatch.setattr(wda_manager, "_default_start_runner", _synchronous_start_runner)


def test_start_wda_uses_real_wda_manager_api(monkeypatch) -> None:
    """A successful reconcile starts WDA through WDAManager.get_or_start.

    RED against the pre-fix code: it called the nonexistent
    ``IOSController().start_wda(serial)``, which raised AttributeError,
    was swallowed, and left ``active_wda`` unset.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager()
    phone = _mock_device(SIM_PHONE_1, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.return_value = _running_session()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([phone])

    mock_wda_manager_instance.get_or_start.assert_called_once_with(
        SIM_PHONE_1, is_physical=False
    )
    assert manager.active_wda == SIM_PHONE_1


def test_start_wda_detects_physical_device_via_real_udid_shape(monkeypatch) -> None:
    """A physical-shaped UDID is forwarded as ``is_physical=True``.

    Round-0 MINOS finding: ``is_physical`` used to be computed from
    ``device.device_type.value == "physical"``, a member that does not exist
    on ``DeviceType`` — always False, so a real physical iPhone got a
    simulator destination. This exercises the real ``_is_physical_udid``
    predicate end to end (not mocked) against a realistic physical UDID.

    linc-nerve#970638ee: physical auto-start is opt-in only, so this UDID
    must be in the allowlist env var or it would now be skipped.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    monkeypatch.setenv("NERVE_WDA_AUTOSTART_PHYSICAL", PHYSICAL_UDID)
    manager = DaemonWDAManager()
    physical = _mock_device(PHYSICAL_UDID, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.return_value = _running_session()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([physical])

    mock_wda_manager_instance.get_or_start.assert_called_once_with(
        PHYSICAL_UDID, is_physical=True
    )
    assert manager.active_wda == PHYSICAL_UDID


def test_reconcile_skips_watchos_only_device_list(monkeypatch) -> None:
    """Given only watchOS devices, no WDA start is attempted."""
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager()
    watch = _mock_device("WATCH-1", device_type="watch")

    mock_wda_manager_cls = MagicMock()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([watch])

    mock_wda_manager_cls.assert_not_called()
    assert manager.active_wda is None


def test_reconcile_prefers_phone_over_watchos(monkeypatch) -> None:
    """Given a watchOS sim and an iOS phone sim, the phone is chosen."""
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager()
    watch = _mock_device("WATCH-1", device_type="watch")
    phone = _mock_device(SIM_PHONE_1, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.return_value = _running_session()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        # Order matters for the "ios_devices[0]" regression this pins.
        manager.reconcile([watch, phone])

    mock_wda_manager_instance.get_or_start.assert_called_once_with(
        SIM_PHONE_1, is_physical=False
    )
    assert manager.active_wda == SIM_PHONE_1


def test_reconcile_watchos_only_never_logs_repeatedly(monkeypatch) -> None:
    """Repeated reconcile calls with only watchOS devices attempt nothing.

    Pins the "no error logged on a repeating cadence" AC for the watchOS
    case specifically: filtering happens before any start attempt, so
    there is nothing to retry or log.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager()
    watch = _mock_device("WATCH-1", device_type="watch")

    mock_wda_manager_cls = MagicMock()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        for _ in range(5):
            manager.reconcile([watch])

    mock_wda_manager_cls.assert_not_called()


def test_reconcile_skips_offline_devices_ahead_of_online_one(monkeypatch) -> None:
    """A realistic device list — several offline sims ahead of one online
    phone — starts WDA on the online device, not the head of the list.

    Round-0 MINOS finding: the candidate filter had no ``state`` check, so
    ``candidates[0]`` was normally a shut-down simulator (``discover()``
    returns every *available* sim, booted or not).
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager()
    offline_1 = _mock_device(SIM_OFFLINE_1, device_type="phone", state="offline")
    offline_2 = _mock_device(SIM_OFFLINE_2, device_type="phone", state="offline")
    online = _mock_device(SIM_PHONE_ONLINE, device_type="phone", state="online")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.return_value = _running_session()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([offline_1, offline_2, online])

    mock_wda_manager_instance.get_or_start.assert_called_once_with(
        SIM_PHONE_ONLINE, is_physical=False
    )
    assert manager.active_wda == SIM_PHONE_ONLINE


def test_reconcile_skips_blacklisted_head_and_tries_next_candidate(monkeypatch) -> None:
    """One structurally-failing device does not disable WDA for every other
    device on the bench.

    Round-0 MINOS finding: ``reconcile`` used to ``return`` as soon as
    ``candidates[0]`` was blacklisted, instead of trying ``candidates[1..n]``
    — a single bad device (e.g. an offline-turned-online flaky sim) starved
    every booted device behind it forever.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager()
    bad = _mock_device(SIM_BAD, device_type="phone")
    good = _mock_device(SIM_GOOD, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.side_effect = [
        RuntimeError("boom"),
        _running_session(),
    ]

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([bad, good])  # bad is tried first, fails, blacklisted
        manager.reconcile([bad, good])  # bad is skipped, good is tried

    assert mock_wda_manager_instance.get_or_start.call_count == 2
    assert manager.active_wda == SIM_GOOD


def test_reconcile_gives_up_after_permanent_failure(monkeypatch) -> None:
    """A structurally-failing start is not retried on every 30s poll.

    Simulates ``get_or_start`` raising every time (e.g. missing Xcode,
    unsupported platform) and asserts the daemon does not hammer it once
    it has already failed for that serial.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager()
    phone = _mock_device(SIM_PHONE_1, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.side_effect = RuntimeError("xcodebuild missing")

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([phone])
        manager.reconcile([phone])
        manager.reconcile([phone])

    assert mock_wda_manager_instance.get_or_start.call_count == 1
    assert manager.active_wda is None


def test_reconcile_recovers_after_unplug_replug(monkeypatch) -> None:
    """A failed serial gets a fresh attempt after it disconnects and
    reconnects, even when it was the ONLY device on the bench.

    Round-0 MINOS finding: the give-up-set prune was unreachable in the
    single-device case (the empty-candidates early return fired first), so
    unplug/replug never actually recovered a failed serial despite the PR
    body's claim that it did.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager()
    phone = _mock_device(SIM_PHONE_X, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.side_effect = RuntimeError("boom")

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([phone])
        assert manager._failed_serials == {SIM_PHONE_X}

        manager.reconcile([])  # unplugged
        assert manager._failed_serials == set()

        mock_wda_manager_instance.get_or_start.side_effect = None
        mock_wda_manager_instance.get_or_start.return_value = _running_session()
        manager.reconcile([phone])  # replugged

    assert mock_wda_manager_instance.get_or_start.call_count == 2
    assert manager.active_wda == SIM_PHONE_X


def test_reconcile_keeps_wda_running_when_device_still_present(monkeypatch) -> None:
    """Once WDA is active for a still-connected device, a later reconcile
    does not restart it (the "keep it" branch of `_reconcile_active_wda`)."""
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager()
    phone = _mock_device(SIM_PHONE_1, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.return_value = _running_session()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([phone])
        manager.reconcile([phone])

    mock_wda_manager_instance.get_or_start.assert_called_once_with(
        SIM_PHONE_1, is_physical=False
    )
    assert manager.active_wda == SIM_PHONE_1


def test_reconcile_stops_and_restarts_when_active_device_disappears(monkeypatch) -> None:
    """When the previously-active device is no longer present, reconcile
    stops the stale WDA via ``WDAManager.stop`` and starts a fresh one for
    the new candidate (the "device gone" branch of `_reconcile_active_wda`).

    Round-0 MINOS finding: the started ``WDASession`` used to be discarded,
    so nothing ever called ``WDAManager.stop`` — this asserts it is called
    with the OLD serial before the new one is started.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager()
    phone_a = _mock_device(SIM_PHONE_A, device_type="phone")
    phone_b = _mock_device(SIM_PHONE_B, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.return_value = _running_session()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([phone_a])
        assert manager.active_wda == SIM_PHONE_A
        manager.reconcile([phone_b])

    assert manager.active_wda == SIM_PHONE_B
    assert mock_wda_manager_instance.get_or_start.call_count == 2
    mock_wda_manager_instance.stop.assert_called_once_with(SIM_PHONE_A)


def test_reconcile_detects_crashed_wda_and_restarts(monkeypatch) -> None:
    """A dead WDA session is treated as a crash: cleared and re-selected on
    the same poll (the crash-detection branch of `_reconcile_active_wda`).

    Round-0 MINOS finding: the old test hand-set ``manager._active_proc``,
    an attribute production never assigns, so the crash branch was
    unreachable in production. This reaches the crashed state through a
    real reconcile+start instead of assigning a private attribute: the
    FIRST reconcile starts WDA and gets a live session back; flipping that
    session's own ``is_running`` to False (simulating the xcodebuild
    process dying) and reconciling again must detect the crash and start a
    fresh session for the same device.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager()
    phone = _mock_device(SIM_PHONE_1, device_type="phone")

    live_session = _running_session()
    restarted_session = _running_session()

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.side_effect = [live_session, restarted_session]

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([phone])
        assert manager.active_wda == SIM_PHONE_1
        assert manager._active_session is live_session

        live_session.is_running = False  # the xcodebuild process died
        manager.reconcile([phone])

    assert mock_wda_manager_instance.get_or_start.call_count == 2
    assert manager.active_wda == SIM_PHONE_1
    assert manager._active_session is restarted_session


def test_shutdown_stops_active_wda(monkeypatch) -> None:
    """``shutdown()`` calls ``WDAManager.stop`` with the active serial.

    Companion to the "device disappears" stop assertion above — the other
    call site the round-0 MINOS finding named explicitly.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager()
    phone = _mock_device(SIM_PHONE_1, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.return_value = _running_session()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([phone])
        manager.shutdown()

    mock_wda_manager_instance.stop.assert_called_once_with(SIM_PHONE_1)
    assert manager.active_wda is None


def test_reconcile_no_project_dir_does_not_construct_wda_manager(monkeypatch) -> None:
    """Without WDA_PROJECT_DIR set, no attempt is made at all (pre-existing guard)."""
    monkeypatch.delenv("WDA_PROJECT_DIR", raising=False)
    manager = DaemonWDAManager()
    phone = _mock_device(SIM_PHONE_1, device_type="phone")

    mock_wda_manager_cls = MagicMock()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([phone])

    mock_wda_manager_cls.assert_not_called()


# --- linc-nerve-nte.1 (reactor 970638ee): re-check live simulator state,
# gate physical auto-start behind an opt-in allowlist, and clean up a
# simulator a failed start left booted. ---


def test_reconcile_skips_simulator_not_actually_booted(monkeypatch) -> None:
    """A candidate the poll list reports "online" but whose live `sim_state`
    is "Shutdown" is skipped: `get_or_start` is never called, the serial is
    NOT blacklisted (so it is retried once something else boots it), and
    the skip is logged.

    Pins the core linc-nerve-nte.1 fix: the poll-derived candidate list can
    be minutes stale, so `reconcile` must re-check LIVE state immediately
    before attempting a start. Fails pre-fix: `get_or_start` was called.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager(sim_states=lambda: {SIM_NOT_BOOTED: "Shutdown"})
    stale_online = _mock_device(SIM_NOT_BOOTED, device_type="phone", state="online")

    mock_wda_manager_cls = MagicMock()

    with (
        patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls),
        structlog.testing.capture_logs() as cap_logs,
    ):
        manager.reconcile([stale_online])

    mock_wda_manager_cls.assert_not_called()
    assert manager.active_wda is None
    assert SIM_NOT_BOOTED not in manager._failed_serials
    skip_events = [
        e
        for e in cap_logs
        if e["event"] == "wda_autostart_skipped" and e.get("reason") == "simulator_not_booted"
    ]
    assert len(skip_events) == 1
    assert skip_events[0]["serial"] == SIM_NOT_BOOTED


def test_reconcile_starts_simulator_when_live_state_is_booted(monkeypatch) -> None:
    """A candidate whose `sim_state` reports "Booted" starts WDA exactly
    once — the nightly attach path still works after the live re-check."""
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager(sim_states=lambda: {SIM_PHONE_1: "Booted"})
    phone = _mock_device(SIM_PHONE_1, device_type="phone", state="online")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.return_value = _running_session()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([phone])

    mock_wda_manager_instance.get_or_start.assert_called_once_with(
        SIM_PHONE_1, is_physical=False
    )
    assert manager.active_wda == SIM_PHONE_1


def test_reconcile_skips_physical_device_without_allowlist(monkeypatch) -> None:
    """A physical UDID with NERVE_WDA_AUTOSTART_PHYSICAL unset is skipped:
    no `get_or_start` call, and the serial is not blacklisted.

    Fails pre-fix: physical auto-start had no opt-in gate at all.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    monkeypatch.delenv("NERVE_WDA_AUTOSTART_PHYSICAL", raising=False)
    manager = DaemonWDAManager()
    physical = _mock_device(PHYSICAL_UDID_DENIED, device_type="phone")

    mock_wda_manager_cls = MagicMock()

    with (
        patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls),
        structlog.testing.capture_logs() as cap_logs,
    ):
        manager.reconcile([physical])

    mock_wda_manager_cls.assert_not_called()
    assert manager.active_wda is None
    assert PHYSICAL_UDID_DENIED not in manager._failed_serials
    skip_events = [
        e
        for e in cap_logs
        if e["event"] == "wda_autostart_skipped" and e.get("reason") == "physical_not_allowlisted"
    ]
    assert len(skip_events) == 1
    assert skip_events[0]["serial"] == PHYSICAL_UDID_DENIED


def test_reconcile_starts_allowlisted_physical_device(monkeypatch) -> None:
    """A physical UDID present in NERVE_WDA_AUTOSTART_PHYSICAL is started
    with `is_physical=True`."""
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    monkeypatch.setenv("NERVE_WDA_AUTOSTART_PHYSICAL", f"OTHER-UDID,{PHYSICAL_UDID}")
    manager = DaemonWDAManager()
    physical = _mock_device(PHYSICAL_UDID, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.return_value = _running_session()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([physical])

    mock_wda_manager_instance.get_or_start.assert_called_once_with(
        PHYSICAL_UDID, is_physical=True
    )
    assert manager.active_wda == PHYSICAL_UDID


def test_start_wda_cleans_up_simulator_a_failed_start_booted(monkeypatch) -> None:
    """A simulator that was "Shutdown" before a (forced) failed start and
    "Booted" after has `sim_shutdown` called once for that UDID, and the
    serial ends up in `_failed_serials`.

    Calls `_start_wda` directly (bypassing `reconcile`'s own live-state
    gate) to force the defence-in-depth path: the state check inside
    `_start_wda` itself must independently notice the stray boot and clean
    it up rather than assuming its caller already guaranteed "Booted".
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    sim_shutdown = MagicMock()
    manager = DaemonWDAManager(
        # `state_before` now comes from the snapshot passed in below, not a
        # fresh `sim_state` call — the only production call left on this
        # seam is the single post-failure re-check `_cleanup_stray_boot`
        # makes, which is what reports "Booted" here.
        sim_state=lambda udid: "Booted",
        sim_shutdown=sim_shutdown,
    )

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.side_effect = RuntimeError("boom")

    with (
        patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls),
        structlog.testing.capture_logs() as cap_logs,
    ):
        manager._start_wda(
            SIM_STALE_CLEANUP, is_physical=False, sim_states={SIM_STALE_CLEANUP: "Shutdown"}
        )

    sim_shutdown.assert_called_once_with(SIM_STALE_CLEANUP)
    assert SIM_STALE_CLEANUP in manager._failed_serials
    cleanup_events = [e for e in cap_logs if e["event"] == "wda_autostart_cleanup"]
    assert len(cleanup_events) == 1
    assert cleanup_events[0]["serial"] == SIM_STALE_CLEANUP


def test_start_wda_refuses_when_wda_products_missing(monkeypatch) -> None:
    """`_start_wda` refuses before any simctl/xcodebuild call when
    WDA_PROJECT_DIR is set but the prebuilt products (no `*.xctestrun`
    under the build products dir) are missing — logs
    `wda_autostart_skipped reason=wda_products_missing` and adds the serial
    to `_failed_serials` without booting anything.

    Fails pre-fix: the guard only checked the env var was non-empty.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager(wda_products_available=lambda is_physical: False)

    mock_wda_manager_cls = MagicMock()

    with (
        patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls),
        structlog.testing.capture_logs() as cap_logs,
    ):
        manager._start_wda(SIM_PRODUCTS_MISSING, is_physical=False)

    mock_wda_manager_cls.assert_not_called()
    assert manager.active_wda is None
    assert SIM_PRODUCTS_MISSING in manager._failed_serials
    skip_events = [
        e
        for e in cap_logs
        if e["event"] == "wda_autostart_skipped" and e.get("reason") == "wda_products_missing"
    ]
    assert len(skip_events) == 1
    assert skip_events[0]["serial"] == SIM_PRODUCTS_MISSING


def test_reconcile_single_active_session_starts_nothing_new(monkeypatch) -> None:
    """Existing single-active-WDA-session invariant, re-pinned alongside the
    new gating logic: with an active running session on a still-present
    serial, reconcile starts nothing new (no extra `get_or_start` calls, no
    live `sim_states` re-check needed to keep it running)."""
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    manager = DaemonWDAManager(sim_states=lambda: {SIM_PHONE_1: "Booted"})
    phone = _mock_device(SIM_PHONE_1, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.return_value = _running_session()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([phone])
        manager.reconcile([phone])
        manager.reconcile([phone])

    mock_wda_manager_instance.get_or_start.assert_called_once_with(
        SIM_PHONE_1, is_physical=False
    )
    assert manager.active_wda == SIM_PHONE_1


# --- linc-nerve-244.1 (reactor 970638ee): one simctl listing per tick, and
# a WDA start runs off a background thread instead of the shared tool
# worker. ---


def _real_thread_runner(target):
    """`start_runner` override that spawns a REAL background thread.

    The pre-existing suite's autouse fixture defaults `start_runner` to a
    synchronous stand-in (see `_synchronous_start_runner` above) so ordinary
    tests can assert state immediately after `reconcile()` without a race.
    The tests below are specifically about the async behavior, so they must
    opt back into the real `threading.Thread`-backed seam.
    """
    return threading.Thread(target=target, daemon=True)


def test_reconcile_calls_sim_states_once_for_many_candidates() -> None:
    """Ten online simulator candidates, none Booted: `reconcile` takes ONE
    `sim_states` snapshot for the whole tick and never falls back to a
    per-UDID probe.

    Fails pre-fix: the old per-candidate `sim_state(udid)` loop issued ten
    (or more) separate `simctl` listings — one per candidate.
    """
    sim_states_mock = MagicMock(return_value={})  # nobody Booted
    sim_state_mock = MagicMock(side_effect=AssertionError("per-UDID seam must not be used"))
    manager = DaemonWDAManager(sim_state=sim_state_mock, sim_states=sim_states_mock)
    candidates = [
        _mock_device(f"AAAAAAAA-1111-2222-3333-4444444444{i:02d}", device_type="phone")
        for i in range(10, 20)
    ]

    mock_wda_manager_cls = MagicMock()

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile(candidates)

    assert sim_states_mock.call_count == 1
    sim_state_mock.assert_not_called()
    mock_wda_manager_cls.assert_not_called()
    assert manager.active_wda is None


def test_reconcile_returns_immediately_while_start_in_flight(monkeypatch) -> None:
    """With a fake `get_or_start` that blocks on a `threading.Event`,
    `reconcile` returns in well under one second while the start is still in
    flight.

    Fails pre-fix: `reconcile` called `get_or_start` inline and blocked
    until it returned.
    """
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    event = threading.Event()
    threads: list[threading.Thread] = []

    def _runner(target):
        t = _real_thread_runner(target)
        threads.append(t)
        return t

    def _blocking_get_or_start(serial, is_physical):
        assert event.wait(timeout=5), "test event was never released"
        return _running_session()

    manager = DaemonWDAManager(
        sim_states=lambda: {SIM_PHONE_1: "Booted"},
        start_runner=_runner,
    )
    phone = _mock_device(SIM_PHONE_1, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.side_effect = _blocking_get_or_start

    try:
        with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
            started = time.monotonic()
            manager.reconcile([phone])
            elapsed = time.monotonic() - started

            assert elapsed < 0.5
            assert manager._starting_serial == SIM_PHONE_1
            assert manager.active_wda is None
    finally:
        event.set()
        for t in threads:
            t.join(timeout=2)


def test_reconcile_while_starting_ignores_new_candidate(monkeypatch) -> None:
    """While a start is in flight, a second `reconcile` tick with another
    Booted candidate starts nothing and does not call `sim_states` again for
    the in-flight serial."""
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    event = threading.Event()
    threads: list[threading.Thread] = []

    def _runner(target):
        t = _real_thread_runner(target)
        threads.append(t)
        return t

    def _blocking_get_or_start(serial, is_physical):
        assert event.wait(timeout=5), "test event was never released"
        return _running_session()

    sim_states_mock = MagicMock(return_value={SIM_PHONE_A: "Booted", SIM_PHONE_B: "Booted"})
    manager = DaemonWDAManager(sim_states=sim_states_mock, start_runner=_runner)
    phone_a = _mock_device(SIM_PHONE_A, device_type="phone")
    phone_b = _mock_device(SIM_PHONE_B, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.side_effect = _blocking_get_or_start

    try:
        with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
            manager.reconcile([phone_a])
            assert sim_states_mock.call_count == 1

            manager.reconcile([phone_a, phone_b])

            assert sim_states_mock.call_count == 1
            assert mock_wda_manager_instance.get_or_start.call_count == 1
            assert manager._starting_serial == SIM_PHONE_A
            assert manager.active_wda is None
    finally:
        event.set()
        for t in threads:
            t.join(timeout=2)


def test_start_success_after_event_release_sets_active(monkeypatch) -> None:
    """Releasing the blocking `Event` with a successful return sets the
    active serial/session once the background thread completes."""
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    event = threading.Event()
    session = _running_session()
    threads: list[threading.Thread] = []

    def _runner(target):
        t = _real_thread_runner(target)
        threads.append(t)
        return t

    def _blocking_get_or_start(serial, is_physical):
        assert event.wait(timeout=5), "test event was never released"
        return session

    manager = DaemonWDAManager(
        sim_states=lambda: {SIM_PHONE_1: "Booted"},
        start_runner=_runner,
    )
    phone = _mock_device(SIM_PHONE_1, device_type="phone")

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.side_effect = _blocking_get_or_start

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager.reconcile([phone])
        assert manager.active_wda is None  # still in flight

        event.set()
        assert threads, "start_runner was never invoked"
        threads[-1].join(timeout=2)

    assert manager.active_wda == SIM_PHONE_1
    assert manager._active_session is session
    assert manager._starting_serial is None


def test_start_failure_after_event_release_fails_serial_and_cleans_up(monkeypatch) -> None:
    """Releasing the blocking `Event` with an exception adds the serial to
    `_failed_serials` and, when the snapshot said Shutdown-before and a
    single post-failure read says Booted, calls `sim_shutdown` once."""
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")
    event = threading.Event()
    threads: list[threading.Thread] = []

    def _runner(target):
        t = _real_thread_runner(target)
        threads.append(t)
        return t

    def _blocking_get_or_start(serial, is_physical):
        assert event.wait(timeout=5), "test event was never released"
        raise RuntimeError("boom")

    sim_shutdown = MagicMock()
    manager = DaemonWDAManager(
        # The single post-failure re-check — the only production call left
        # on this seam — reports the simulator came up Booted anyway.
        sim_state=lambda udid: "Booted",
        sim_shutdown=sim_shutdown,
        start_runner=_runner,
    )

    mock_wda_manager_cls = MagicMock()
    mock_wda_manager_instance = mock_wda_manager_cls.return_value
    mock_wda_manager_instance.get_or_start.side_effect = _blocking_get_or_start

    with patch("linc_nerve.core.wda.WDAManager", mock_wda_manager_cls):
        manager._start_wda(
            SIM_STALE_CLEANUP, is_physical=False, sim_states={SIM_STALE_CLEANUP: "Shutdown"}
        )
        assert SIM_STALE_CLEANUP not in manager._failed_serials  # still in flight

        event.set()
        assert threads, "start_runner was never invoked"
        threads[-1].join(timeout=2)

    sim_shutdown.assert_called_once_with(SIM_STALE_CLEANUP)
    assert SIM_STALE_CLEANUP in manager._failed_serials
    assert manager._starting_serial is None
    assert manager.active_wda is None
