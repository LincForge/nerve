"""Tests for WDA lifecycle management in the daemon."""

from unittest.mock import ANY, MagicMock, patch

from linc_nerve.daemon.wda_manager import DaemonWDAManager


def test_wda_not_started_without_ios_devices() -> None:
    """WDA doesn't start if no iOS devices discovered."""
    mgr = DaemonWDAManager()
    mgr.reconcile(ios_devices=[])
    assert mgr.active_wda is None


def test_wda_starts_for_first_ios_simulator() -> None:
    """WDA starts when an iOS simulator is discovered."""
    mock_device = MagicMock()
    # Simulator-UUID-shaped (36 chars, 4 dashes) so `_is_physical_udid`
    # correctly reports this as a simulator, not a physical device
    # (linc-nerve#137 round-0 finding: a non-UUID-shaped serial used to be
    # meaningless here because `is_physical` was computed from a bogus
    # DeviceType member and was always False regardless of serial shape).
    mock_device.serial = "AAAAAAAA-1111-2222-3333-444444444410"
    mock_device.device_type = MagicMock(value="simulator")
    mock_device.state = "online"

    with patch.object(DaemonWDAManager, "_start_wda") as mock_start:
        # `sim_states` seam pinned to Booted (linc-nerve#970638ee): reconcile
        # now takes one live-state snapshot per tick before calling
        # `_start_wda`, which is mocked out here — without this the real
        # seam would shell out to `xcrun simctl`.
        mgr = DaemonWDAManager(
            sim_states=lambda: {"AAAAAAAA-1111-2222-3333-444444444410": "Booted"}
        )
        mgr.reconcile(ios_devices=[mock_device])
        mock_start.assert_called_once_with(
            "AAAAAAAA-1111-2222-3333-444444444410", is_physical=False, sim_states=ANY
        )


def test_wda_restarts_on_crash() -> None:
    """WDA restarts if the WDASession reports it is no longer running.

    Reaches the crashed state via a real `_active_session` whose
    `is_running` is False, rather than hand-setting a private `_active_proc`
    attribute production never assigns (linc-nerve#137 round-0 finding: the
    old version of this test certified a crash-restart capability that was
    unreachable in production).
    """
    # `sim_states` seam pinned to Booted (linc-nerve#970638ee) — see note
    # above.
    mgr = DaemonWDAManager(
        sim_states=lambda: {"AAAAAAAA-1111-2222-3333-444444444411": "Booted"}
    )
    mgr._active_serial = "AAAAAAAA-1111-2222-3333-444444444411"
    dead_session = MagicMock()
    dead_session.is_running = False
    mgr._active_session = dead_session

    mock_device = MagicMock()
    mock_device.serial = "AAAAAAAA-1111-2222-3333-444444444411"
    mock_device.device_type = MagicMock(value="simulator")
    mock_device.state = "online"

    with patch.object(DaemonWDAManager, "_start_wda") as mock_start:
        mgr.reconcile(ios_devices=[mock_device])
        mock_start.assert_called_once()  # Restarted
