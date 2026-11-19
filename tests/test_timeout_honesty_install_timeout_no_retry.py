"""Tests for nerve long-operation timeout honesty (linc-nerve-uwfd)."""

from __future__ import annotations

import math
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core.adb_path import ADB_PATH
from linc_nerve.core.android import AndroidController
from linc_nerve.core.metrics import MetricsCollector
from linc_nerve.mcp import sse_server


def _make_controller() -> AndroidController:
    ctrl = AndroidController()
    ctrl._scrcpy_available = False
    return ctrl


def _completed(returncode: int = 0, stdout: str = "Success\n", stderr: str = "") -> MagicMock:
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def test_install_timeout_no_retry_and_honest_message() -> None:
    """[contract] TimeoutExpired from adb install is not retried and reports an honest message."""
    ctrl = _make_controller()
    mc = MetricsCollector()
    ctrl._metrics = mc

    with (
        patch(
            "linc_nerve.core.android.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="adb", timeout=120),
        ) as mock_run,
        patch("linc_nerve.core.android.time.sleep") as mock_sleep,
        pytest.raises(TimeoutError) as exc_info,
    ):
        ctrl.install("ABC", "/tmp/app.apk")

    install_calls = [c for c in mock_run.call_args_list if c.args and "install" in c.args[0]]
    assert len(install_calls) == 1
    assert install_calls[0].args[0] == [
        ADB_PATH,
        "-s",
        "ABC",
        "install",
        "-r",
        "-t",
        "/tmp/app.apk",
    ]
    assert install_calls[0].kwargs.get("timeout") == 120
    assert install_calls[0].kwargs.get("capture_output") is True
    assert install_calls[0].kwargs.get("text") is True
    mock_sleep.assert_not_called()

    msg = str(exc_info.value)
    assert msg == (
        "adb install timed out after 120s; the install may still be completing "
        "on the device. Check nerve_apps for the package before retrying."
    )
    assert "may still be completing" in msg
    assert "nerve_apps" in msg

    recent = mc.recent(n=10)
    assert len(recent) == 1
    assert recent[0]["command"] == "install"
    assert recent[0]["serial"] == "ABC"
    assert recent[0]["duration_ms"] >= 0
    assert recent[0]["success"] is False
    assert "may still be completing" in recent[0]["error"]
    assert mc.stats()["install"]["errors_by_category"] == {"timeout": 1}


def test_install_uses_timeouts_table_inner_budget() -> None:
    """[contract] _adb_install reads its subprocess timeout from timeouts.INNER_BUDGET_S."""
    from linc_nerve.core import timeouts

    ctrl = _make_controller()
    with (
        patch.dict(timeouts.INNER_BUDGET_S, {"nerve_install": 95}),
        patch(
            "linc_nerve.core.android.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="adb", timeout=95),
        ) as mock_run,
        pytest.raises(TimeoutError) as exc_info,
    ):
        ctrl.install("ABC", "/tmp/app.apk")

    install_calls = [c for c in mock_run.call_args_list if c.args and "install" in c.args[0]]
    assert len(install_calls) == 1
    assert install_calls[0].kwargs.get("timeout") == 95
    assert "adb install timed out after 95s; the install may still be completing" in str(
        exc_info.value
    )


def test_timeouts_table_ordering_and_unlisted_defaults() -> None:
    """[contract] Every listed tool obeys inner < mcp <= daemon < cli with positive values."""
    from linc_nerve.core import timeouts

    timeouts.validate_timeouts_table()

    expected_tools = {
        "nerve_install",
        "nerve_record_fixed",
        "nerve_record_stop",
        "nerve_emulator_start",
    }
    assert set(timeouts.INNER_BUDGET_S) == expected_tools
    assert set(timeouts.MCP_CEILING_S) == expected_tools
    assert set(timeouts.CLI_DEFAULT_S) == expected_tools
    assert timeouts.DAEMON_CEILING_S == 180.0
    assert timeouts.UNLISTED_MCP_CEILING_S == 90
    assert timeouts.UNLISTED_CLI_DEFAULT_S == 30

    assert timeouts.inner_budget("nerve_install") == 120
    assert timeouts.mcp_ceiling("nerve_install") == 150
    assert timeouts.cli_default("nerve_install") == 200

    assert timeouts.inner_budget("nerve_record_fixed") == 150
    assert timeouts.mcp_ceiling("nerve_record_fixed") == 160
    assert timeouts.cli_default("nerve_record_fixed") == 200

    assert timeouts.inner_budget("nerve_record_stop") == 120
    assert timeouts.mcp_ceiling("nerve_record_stop") == 150
    assert timeouts.cli_default("nerve_record_stop") == 200

    assert timeouts.inner_budget("nerve_emulator_start") == 120
    assert timeouts.mcp_ceiling("nerve_emulator_start") == 150
    assert timeouts.cli_default("nerve_emulator_start") == 200

    for tool in expected_tools:
        inner = timeouts.inner_budget(tool)
        mcp = timeouts.mcp_ceiling(tool)
        daemon = timeouts.DAEMON_CEILING_S
        cli = timeouts.cli_default(tool)
        for val in (inner, mcp, daemon, cli):
            assert not isinstance(val, bool)
            assert isinstance(val, (int, float))
            assert math.isfinite(val)
            assert val > 0
        assert inner < mcp <= daemon < cli

    # Boundary: inner=1, mcp==daemon (1 < 180 <= 180.0 < 200) is valid.
    with (
        patch.dict(timeouts.INNER_BUDGET_S, {"nerve_install": 1}),
        patch.dict(timeouts.MCP_CEILING_S, {"nerve_install": 180}),
    ):
        assert timeouts.inner_budget("nerve_install") == 1
        assert timeouts.mcp_ceiling("nerve_install") == 180
        timeouts.validate_timeouts_table()

    # Unlisted tools resolve to 90 (mcp) and 30 (cli) (pin).
    assert timeouts.mcp_ceiling("nerve_tap") == 90
    assert timeouts.cli_default("nerve_tap") == 30


@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), float("inf"), float("-inf"), 0, -1, True, "120"],
    ids=["nan", "inf", "neg_inf", "zero", "negative", "bool", "str"],
)
def test_timeouts_table_rejects_non_positive_or_non_finite_values(bad_value: object) -> None:
    """[contract] NaN, inf, 0, negative, bool, or non-numeric entries fail validation."""
    from linc_nerve.core import timeouts

    with patch.dict(timeouts.INNER_BUDGET_S, {"nerve_install": bad_value}):  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="finite positive number"):
            timeouts.inner_budget("nerve_install")
        with pytest.raises(ValueError, match="finite positive number"):
            timeouts.validate_timeouts_table()

    with patch.dict(timeouts.MCP_CEILING_S, {"nerve_install": bad_value}):  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="finite positive number"):
            timeouts.mcp_ceiling("nerve_install")
        with pytest.raises(ValueError, match="finite positive number"):
            timeouts.validate_timeouts_table()

    with patch.dict(timeouts.CLI_DEFAULT_S, {"nerve_install": bad_value}):  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="finite positive number"):
            timeouts.cli_default("nerve_install")
        with pytest.raises(ValueError, match="finite positive number"):
            timeouts.validate_timeouts_table()

    with (
        patch.object(timeouts, "DAEMON_CEILING_S", bad_value),
        pytest.raises(ValueError, match="finite positive number"),
    ):
        timeouts.validate_timeouts_table()


@pytest.mark.parametrize(
    ("inner", "mcp", "daemon", "cli"),
    [
        (150, 150, 180.0, 200),
        (160, 150, 180.0, 200),
        (120, 181, 180.0, 200),
        (120, 150, 180.0, 180),
        (120, 150, 180.0, 170),
    ],
    ids=[
        "inner_eq_mcp",
        "inner_gt_mcp",
        "mcp_gt_daemon",
        "daemon_eq_cli",
        "daemon_gt_cli",
    ],
)
def test_timeouts_table_rejects_layer_ordering_violations(
    inner: int, mcp: int, daemon: float, cli: int
) -> None:
    """[contract] Violation of inner < mcp <= daemon < cli fails validation."""
    from linc_nerve.core import timeouts

    with (
        patch.dict(timeouts.INNER_BUDGET_S, {"nerve_install": inner}),
        patch.dict(timeouts.MCP_CEILING_S, {"nerve_install": mcp}),
        patch.object(timeouts, "DAEMON_CEILING_S", daemon),
        patch.dict(timeouts.CLI_DEFAULT_S, {"nerve_install": cli}),
        pytest.raises(ValueError, match="inner < mcp <= daemon < cli"),
    ):
        timeouts.validate_timeouts_table()


def test_timeouts_table_rejects_key_mismatch() -> None:
    """[contract] Mismatched tool keys across the budget dicts fail validation."""
    from linc_nerve.core import timeouts

    with (
        patch.dict(timeouts.MCP_CEILING_S, {"nerve_extra": 150}),
        pytest.raises(ValueError, match="timeouts table keys must match"),
    ):
        timeouts.validate_timeouts_table()

    with (
        patch.dict(timeouts.CLI_DEFAULT_S, {"nerve_extra": 200}),
        pytest.raises(ValueError, match="timeouts table keys must match"),
    ):
        timeouts.validate_timeouts_table()


def test_install_os_error_still_retried_up_to_three_attempts() -> None:
    """[test] OSError from adb install is still retried up to 3 attempts (pin)."""
    ctrl = _make_controller()
    sleep_calls: list[float] = []

    with (
        patch(
            "linc_nerve.core.android.subprocess.run",
            side_effect=OSError("broken pipe"),
        ) as mock_run,
        patch("linc_nerve.core.android.time.sleep", side_effect=sleep_calls.append),
        pytest.raises(OSError, match="broken pipe"),
    ):
        ctrl.install("ABC", "/tmp/app.apk")

    install_calls = [c for c in mock_run.call_args_list if c.args and "install" in c.args[0]]
    assert len(install_calls) == 3
    assert sleep_calls == [0.5, 1.0]

    # And succeeds if the 3rd attempt recovers:
    ctrl2 = _make_controller()
    with (
        patch(
            "linc_nerve.core.android.subprocess.run",
            side_effect=[OSError("broken pipe"), ConnectionResetError("reset"), _completed()],
        ) as mock_run2,
        patch("linc_nerve.core.android.time.sleep"),
    ):
        ctrl2.install("ABC", "/tmp/app.apk")

    install_calls2 = [c for c in mock_run2.call_args_list if c.args and "install" in c.args[0]]
    assert len(install_calls2) == 3


def test_resolve_tool_timeout_default_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """[test] _resolve_tool_timeout() returns 180.0 and honours NERVE_TOOL_TIMEOUT (pin)."""
    assert sse_server._DEFAULT_TOOL_TIMEOUT == 180.0

    monkeypatch.delenv("NERVE_TOOL_TIMEOUT", raising=False)
    assert sse_server._resolve_tool_timeout() == 180.0

    monkeypatch.setenv("NERVE_TOOL_TIMEOUT", "240.5")
    assert sse_server._resolve_tool_timeout() == 240.5

    monkeypatch.setenv("NERVE_TOOL_TIMEOUT", "invalid")
    assert sse_server._resolve_tool_timeout() == 180.0
