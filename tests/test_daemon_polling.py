"""Tests for daemon device polling loop."""

from unittest.mock import MagicMock

from linc_nerve.daemon.polling import poll_devices


def test_poll_devices_detects_new_device() -> None:
    """New device in adb output is reported as connected."""
    mock_android = MagicMock()
    mock_android.discover.return_value = [
        MagicMock(
            serial="ABC123",
            model="Pixel",
            platform=MagicMock(value="android"),
            device_type=MagicMock(value="phone"),
            os_version="15",
        ),
    ]
    mock_ios = MagicMock()
    mock_ios.discover.return_value = []

    diff = poll_devices(android_ctrl=mock_android, ios_ctrl=mock_ios, known_serials=set())
    assert "ABC123" in diff.connected
    assert len(diff.disconnected) == 0


def test_poll_devices_detects_disconnected() -> None:
    """Device missing from output is reported as disconnected."""
    mock_android = MagicMock()
    mock_android.discover.return_value = []
    mock_ios = MagicMock()
    mock_ios.discover.return_value = []

    diff = poll_devices(android_ctrl=mock_android, ios_ctrl=mock_ios, known_serials={"ABC123"})
    assert "ABC123" in diff.disconnected
    assert len(diff.connected) == 0


def test_poll_devices_no_change() -> None:
    """No diff when device list unchanged."""
    mock_android = MagicMock()
    mock_android.discover.return_value = [
        MagicMock(
            serial="ABC123",
            model="Pixel",
            platform=MagicMock(value="android"),
            device_type=MagicMock(value="phone"),
            os_version="15",
        ),
    ]
    mock_ios = MagicMock()
    mock_ios.discover.return_value = []

    diff = poll_devices(android_ctrl=mock_android, ios_ctrl=mock_ios, known_serials={"ABC123"})
    assert len(diff.connected) == 0
    assert len(diff.disconnected) == 0


def test_poll_devices_android_error_continues_ios() -> None:
    """If Android discovery fails, iOS devices are still returned."""
    mock_android = MagicMock()
    mock_android.discover.side_effect = RuntimeError("adb crashed")
    mock_ios = MagicMock()
    mock_ios.discover.return_value = [
        MagicMock(
            serial="IOS123",
            model="iPhone",
            platform=MagicMock(value="ios"),
            device_type=MagicMock(value="phone"),
            os_version="17",
        ),
    ]

    diff = poll_devices(android_ctrl=mock_android, ios_ctrl=mock_ios, known_serials=set())
    assert "IOS123" in diff.connected
    assert len(diff.disconnected) == 0


def _mock_device(serial: str, platform: str = "ios") -> MagicMock:
    return MagicMock(
        serial=serial,
        model="iPhone" if platform == "ios" else "Pixel",
        platform=MagicMock(value=platform),
        device_type=MagicMock(value="phone"),
        os_version="17" if platform == "ios" else "15",
    )


class TestRawDeviceLists:
    """poll_devices exposes per-controller discovered device lists (issue #84)."""

    def test_poll_devices_exposes_raw_device_lists(self) -> None:
        """Callers can reuse the discovered device objects without re-discovering."""
        android_dev = _mock_device("ABC123", platform="android")
        ios_dev = _mock_device("IOS123", platform="ios")
        mock_android = MagicMock()
        mock_android.discover.return_value = [android_dev]
        mock_ios = MagicMock()
        mock_ios.discover.return_value = [ios_dev]

        diff = poll_devices(
            android_ctrl=mock_android, ios_ctrl=mock_ios, known_serials=set()
        )
        assert diff.raw_devices["android"] == [android_dev]
        assert diff.raw_devices["ios"] == [ios_dev]

    def test_poll_devices_failed_discovery_omits_platform_key(self) -> None:
        """A failed discover() leaves that platform absent from raw_devices."""
        mock_android = MagicMock()
        mock_android.discover.return_value = []
        mock_ios = MagicMock()
        mock_ios.discover.side_effect = RuntimeError("simctl crashed")

        diff = poll_devices(
            android_ctrl=mock_android, ios_ctrl=mock_ios, known_serials=set()
        )
        assert "android" in diff.raw_devices
        assert "ios" not in diff.raw_devices


class TestPollCycleSingleDiscover:
    """_poll_cycle reuses the polled iOS list for WDA reconcile (issue #84)."""

    def test_poll_cycle_single_ios_discover_per_tick(self) -> None:
        """Exactly ONE ios.discover() call per tick."""
        from linc_nerve.cli.main import _poll_cycle

        mock_android = MagicMock()
        mock_android.discover.return_value = []
        mock_ios = MagicMock()
        mock_ios.discover.return_value = [_mock_device("IOS123")]
        wda_mgr = MagicMock()

        _poll_cycle(mock_android, mock_ios, wda_mgr, set())
        assert mock_ios.discover.call_count == 1

    def test_poll_cycle_reconcile_receives_polled_devices(self) -> None:
        """Regression: reconcile receives the same device set as today."""
        from linc_nerve.cli.main import _poll_cycle

        ios_dev = _mock_device("IOS123")
        mock_android = MagicMock()
        mock_android.discover.return_value = []
        mock_ios = MagicMock()
        mock_ios.discover.return_value = [ios_dev]
        wda_mgr = MagicMock()

        _poll_cycle(mock_android, mock_ios, wda_mgr, set())
        wda_mgr.reconcile.assert_called_once_with([ios_dev])

    def test_poll_cycle_freshness_two_ticks(self) -> None:
        """Reconcile consumes the CURRENT tick's list across consecutive ticks."""
        from linc_nerve.cli.main import _poll_cycle

        dev_a = _mock_device("IOS-A")
        dev_b = _mock_device("IOS-B")
        mock_android = MagicMock()
        mock_android.discover.return_value = []
        mock_ios = MagicMock()
        mock_ios.discover.side_effect = [[dev_a], [dev_b]]
        wda_mgr = MagicMock()

        diff1 = _poll_cycle(mock_android, mock_ios, wda_mgr, set())
        _poll_cycle(mock_android, mock_ios, wda_mgr, diff1.all_serials)

        assert wda_mgr.reconcile.call_args_list[0].args[0] == [dev_a]
        assert wda_mgr.reconcile.call_args_list[1].args[0] == [dev_b]

    def test_poll_cycle_ios_discover_error_skips_reconcile(self) -> None:
        """Failed iOS discovery skips reconcile (preserves current semantics)."""
        from linc_nerve.cli.main import _poll_cycle

        mock_android = MagicMock()
        mock_android.discover.return_value = []
        mock_ios = MagicMock()
        mock_ios.discover.side_effect = RuntimeError("simctl crashed")
        wda_mgr = MagicMock()

        diff = _poll_cycle(mock_android, mock_ios, wda_mgr, set())
        wda_mgr.reconcile.assert_not_called()
        assert diff is not None

    def test_poll_cycle_reconcile_error_is_swallowed(self) -> None:
        """A reconcile failure never breaks the poll cycle (preserves current semantics)."""
        from linc_nerve.cli.main import _poll_cycle

        mock_android = MagicMock()
        mock_android.discover.return_value = []
        mock_ios = MagicMock()
        mock_ios.discover.return_value = [_mock_device("IOS123")]
        wda_mgr = MagicMock()
        wda_mgr.reconcile.side_effect = RuntimeError("wda boom")

        diff = _poll_cycle(mock_android, mock_ios, wda_mgr, set())
        assert diff is not None
