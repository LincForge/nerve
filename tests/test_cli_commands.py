"""Unit tests for direct nerve CLI subcommands.

The hand-written commands route through ``linc_nerve.cli.main._call`` (the shared
linc-cli-kit transport). JSON is the default output mode, so the human-text assertions
below invoke with ``--human``; failures produce an empty stdout plus a stderr envelope.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from linc_nerve.cli.main import main

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")


def test_cli_screenshot_success(tmp_path: object) -> None:
    runner = CliRunner()
    fake_shot = str(tmp_path / "fake.png")
    with open(fake_shot, "wb") as f:
        f.write(b"PNG_DATA")

    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"screenshot": fake_shot}
        out_target = str(tmp_path / "out.png")
        result = runner.invoke(main, ["--human", "screenshot", "DEV123", "-o", out_target])
        assert result.exit_code == 0
        assert f"Screenshot saved to: {out_target}" in result.output
        mock_call.assert_called_once_with("nerve_screenshot", {"device": "DEV123"})


def test_cli_screenshot_error() -> None:
    runner = CliRunner()
    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"error": "Device offline"}
        result = runner.invoke(main, ["screenshot", "DEV123"])
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Device offline" in result.stderr


def test_cli_screenshot_json_is_default() -> None:
    runner = CliRunner()
    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"screenshot": "/tmp/x.png"}
        result = runner.invoke(main, ["screenshot", "DEV123"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"screenshot": "/tmp/x.png"}


def test_cli_launch_success() -> None:
    runner = CliRunner()
    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"success": True}
        result = runner.invoke(main, ["--human", "launch", "DEV123", "com.example.app"])
        assert result.exit_code == 0
        assert "Launched com.example.app on DEV123" in result.output
        mock_call.assert_called_once_with(
            "nerve_launch", {"device": "DEV123", "package": "com.example.app"}
        )


def test_cli_kill_success() -> None:
    runner = CliRunner()
    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"success": True}
        result = runner.invoke(main, ["--human", "kill", "DEV123", "com.example.app"])
        assert result.exit_code == 0
        assert "Killed com.example.app on DEV123" in result.output
        mock_call.assert_called_once_with(
            "nerve_kill", {"device": "DEV123", "package": "com.example.app"}
        )


def test_cli_elements_success() -> None:
    runner = CliRunner()
    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"elements": [{"id": "btn", "text": "Submit"}]}
        result = runner.invoke(main, ["--human", "elements", "DEV123"])
        assert result.exit_code == 0
        assert "Submit" in result.output
        mock_call.assert_called_once_with("nerve_elements", {"device": "DEV123"})


def test_cli_tap_success() -> None:
    runner = CliRunner()
    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"success": True}
        result = runner.invoke(main, ["--human", "tap", "DEV123", "200", "400"])
        assert result.exit_code == 0
        assert "Tapped (200, 400) on DEV123" in result.output
        mock_call.assert_called_once_with("nerve_tap", {"device": "DEV123", "x": 200, "y": 400})


def test_cli_discover_routes_through_transport() -> None:
    runner = CliRunner()
    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {
            "devices": [
                {
                    "serial": "DEV123",
                    "platform": "android",
                    "device_type": "phone",
                    "model": "Pixel",
                    "os_version": "14",
                }
            ],
            "count": 1,
        }
        result = runner.invoke(main, ["--human", "discover"])
        assert result.exit_code == 0
        assert "DEV123" in result.output
        assert "1 device(s) found." in result.output
        mock_call.assert_called_once_with("nerve_discover", {})


class _RefusingClient:
    """A daemon that is down: every POST raises, which forces the local fallback path."""

    def __enter__(self) -> _RefusingClient:
        return self

    def __exit__(self, *a: object) -> bool:
        return False

    def post(self, url: str, json: object = None, timeout: object = None) -> object:
        raise httpx.ConnectError("refused")


def _invoke_via_local_fallback(monkeypatch: pytest.MonkeyPatch, raiser: object) -> object:
    """Run `nerve apps --device DEV123` with the daemon down and `raiser` as the tool fn."""
    from linc_nerve.cli import main as cli_main
    from linc_nerve.mcp import sse_server

    monkeypatch.setattr(cli_main.TRANSPORT, "_client_factory", _RefusingClient)
    monkeypatch.setattr(cli_main, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(sse_server, "_get_tool_registry", lambda: {"nerve_apps": raiser})
    return CliRunner().invoke(main, ["apps", "--device", "DEV123"])


def test_local_fallback_client_input_error_is_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Daemon-down must give the SAME exit code as daemon-up for a client-input error.

    `daemon/http_router._error_status_and_body` maps ValueError/KeyError/LookupError/TypeError
    to HTTP 400 -> the kit transport raises UsageError -> exit 2. The local fallback has to
    mirror that or the exit code depends on whether the daemon happens to be running.
    """

    def raiser(**_payload: object) -> object:
        raise ValueError("unknown device")

    result = _invoke_via_local_fallback(monkeypatch, raiser)
    assert result.exit_code == 2
    assert result.stdout == ""
    envelope = json.loads(result.stderr)["error"]
    assert envelope["category"] == "validation"
    assert envelope["message"] == "unknown device"


def test_local_fallback_internal_error_is_still_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def raiser(**_payload: object) -> object:
        raise RuntimeError("controller exploded")

    result = _invoke_via_local_fallback(monkeypatch, raiser)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["category"] == "internal"


def test_cli_screenshot_output_flag_without_a_path_is_an_error(tmp_path: object) -> None:
    """-o promises a file; a tool result with no `screenshot` key must not exit 0."""
    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"ok": True}
        result = CliRunner().invoke(
            main, ["screenshot", "DEV123", "-o", str(tmp_path / "out.png")]
        )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["message"] == "No screenshot path returned"


def test_cli_screenshot_without_output_flag_still_succeeds() -> None:
    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"ok": True}
        result = CliRunner().invoke(main, ["screenshot", "DEV123"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"ok": True}


# --- honest CLI error mapping (linc-nerve-zqy.4) --------------------------------------------


class _FakeResponse:
    """A canned httpx.Response stand-in — status/body fixed, matching HttpDaemonTransport's
    reads: `.content` (truthy check), `.json()`, `.status_code`, `.text`."""

    def __init__(self, status_code: int, body: dict[str, object]) -> None:
        self.status_code = status_code
        self._body = body
        self.content = b"1"
        self.text = json.dumps(body)

    def json(self) -> dict[str, object]:
        return self._body


class _FakeDaemonClient:
    """A daemon that is up and answers every POST with a fixed status/body."""

    def __init__(self, status_code: int, body: dict[str, object]) -> None:
        self._status_code = status_code
        self._body = body

    def __enter__(self) -> _FakeDaemonClient:
        return self

    def __exit__(self, *a: object) -> bool:
        return False

    def post(self, url: str, json: object = None, timeout: object = None) -> _FakeResponse:
        return _FakeResponse(self._status_code, self._body)


def _stub_daemon(
    monkeypatch: pytest.MonkeyPatch, status_code: int, body: dict[str, object]
) -> None:
    from linc_nerve.cli import main as cli_main

    monkeypatch.setattr(
        cli_main.TRANSPORT, "_client_factory", lambda: _FakeDaemonClient(status_code, body)
    )


_DEVICE_NOT_FOUND_MSG = "Device 'BOGUS123' not found. Run nerve_discover to list devices."


def test_cli_logcat_device_not_found_via_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 daemon response carrying a device-not-found error dict maps to DEVICE_NOT_FOUND."""
    _stub_daemon(monkeypatch, 200, {"result": {"error": _DEVICE_NOT_FOUND_MSG}})

    result = CliRunner().invoke(main, ["logcat", "--device", "BOGUS123"])

    assert result.exit_code == 2
    assert result.stdout == ""
    envelope = json.loads(result.stderr)["error"]
    assert envelope["code"] == "DEVICE_NOT_FOUND"
    assert envelope["category"] == "not_found"
    assert "nerve discover" in envelope["hint"]


def test_cli_logcat_device_not_found_via_local_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Daemon-down local fallback must classify the SAME error dict identically (parity)."""
    from linc_nerve.cli import main as cli_main
    from linc_nerve.mcp import sse_server

    def raiser(**_payload: object) -> dict[str, object]:
        return {"error": _DEVICE_NOT_FOUND_MSG}

    monkeypatch.setattr(cli_main.TRANSPORT, "_client_factory", _RefusingClient)
    monkeypatch.setattr(cli_main, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(sse_server, "_get_tool_registry", lambda: {"nerve_logcat": raiser})

    result = CliRunner().invoke(main, ["logcat", "--device", "BOGUS123"])

    assert result.exit_code == 2
    assert result.stdout == ""
    envelope = json.loads(result.stderr)["error"]
    assert envelope["code"] == "DEVICE_NOT_FOUND"
    assert envelope["category"] == "not_found"
    assert "nerve discover" in envelope["hint"]


def test_cli_tap_device_not_found_via_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hand-written positional `tap` command inherits the mapping via `_call` -> TRANSPORT."""
    _stub_daemon(monkeypatch, 200, {"result": {"error": _DEVICE_NOT_FOUND_MSG}})

    result = CliRunner().invoke(main, ["tap", "BOGUS123", "1", "1"])

    assert result.exit_code == 2
    envelope = json.loads(result.stderr)["error"]
    assert envelope["category"] == "not_found"


def test_cli_record_stop_device_not_found_via_daemon_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon's 400 UsageError mapping for a bad --device is re-mapped to DEVICE_NOT_FOUND."""
    message = "Device not found: Device 'X' not found. Run nerve_discover to list devices."
    _stub_daemon(monkeypatch, 400, {"error": message})

    result = CliRunner().invoke(main, ["record-stop", "--device", "X"])

    assert result.exit_code == 2
    envelope = json.loads(result.stderr)["error"]
    assert envelope["code"] == "DEVICE_NOT_FOUND"


def test_cli_record_stop_daemon_down_is_still_daemon_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PIN: a daemon-only tool with the daemon down still gets DAEMON_UNAVAILABLE, unaffected
    by the NerveTransport subclass."""
    from linc_nerve.cli import main as cli_main

    monkeypatch.setattr(cli_main.TRANSPORT, "_client_factory", _RefusingClient)

    result = CliRunner().invoke(main, ["record-stop", "--device", "X"])

    assert result.exit_code == 1
    envelope = json.loads(result.stderr)["error"]
    assert envelope["code"] == "DAEMON_UNAVAILABLE"
    assert envelope["category"] == "unavailable"
    assert envelope["retryable"] is True
    assert "nerve daemon" in envelope["hint"]
