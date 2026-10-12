"""Unit tests for linc_nerve.cli.errors — pure mapping of tool-result dicts / kit exceptions
to precise linc_cli_kit.ToolError instances (see linc-nerve-zqy.4).
"""

from __future__ import annotations

from linc_cli_kit import ToolError, UsageError

from linc_nerve.cli.errors import cli_hint, honest_error, honest_toolerror


def test_honest_error_device_not_found() -> None:
    err = honest_error(
        {"error": "Device 'BOGUS123' not found. Run nerve_discover to list devices."}
    )
    assert isinstance(err, ToolError)
    assert err.code == "DEVICE_NOT_FOUND"
    assert err.category == "not_found"
    assert err.retryable is False
    assert err.exit_code == 2
    assert "`nerve discover`" in err.hint
    assert "nerve_discover" not in err.hint
    assert err.details == {"device": "BOGUS123"}


def test_honest_error_device_reserved() -> None:
    err = honest_error(
        {
            "error": (
                "Device 'S' is reserved by another session (session x, pid 1, "
                "purpose: none). Try again later."
            )
        }
    )
    assert isinstance(err, ToolError)
    assert err.code == "DEVICE_RESERVED"
    assert err.category == "unavailable"
    assert err.retryable is True


def test_honest_error_unsupported() -> None:
    err = honest_error(
        {"error": "unsupported: WDA not supported on watchOS device X", "unsupported": True}
    )
    assert isinstance(err, ToolError)
    assert err.code == "UNSUPPORTED"
    assert err.category == "validation"


def test_honest_error_none_for_generic_error() -> None:
    assert honest_error({"error": "Device offline"}) is None


def test_honest_error_none_for_success() -> None:
    assert honest_error({"ok": True}) is None


def test_honest_error_none_for_non_dict() -> None:
    assert honest_error("not a dict") is None
    assert honest_error(None) is None


def test_cli_hint_rewrites_mcp_tool_names() -> None:
    assert (
        cli_hint("Run nerve_discover, then nerve_clear_app_data")
        == "Run nerve discover, then nerve clear-app-data"
    )


def test_honest_toolerror_remaps_device_not_found_usage_error() -> None:
    exc = UsageError("Device not found: Device 'X' not found. Run nerve_discover to list devices.")
    mapped = honest_toolerror(exc)
    assert mapped.code == "DEVICE_NOT_FOUND"
    assert mapped.category == "not_found"
    assert mapped.details == {"device": "X"}


def test_honest_toolerror_passthrough_for_other_errors() -> None:
    exc = ToolError("daemon exploded", code="DAEMON_ERROR")
    assert honest_toolerror(exc) is exc


def test_held_by_guard_message_classified_as_reserved() -> None:
    err = honest_error(
        {
            "error": "Device 'S' is held by seat-x since 2026-01-01T00:00:00+00:00 "
            "(purpose: unspecified). Use nerve_pool to see all reservations."
        }
    )
    assert isinstance(err, ToolError)
    assert err.code == "DEVICE_RESERVED"
