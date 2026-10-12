"""Tests for MCP call observability logger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from linc_nerve.core.observability import log_mcp_call


class TestLogMcpCall:
    def test_writes_jsonl_entry(self, tmp_path: Path) -> None:
        log_file = tmp_path / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-nerve",
            tool="nerve_screenshot",
            params={"device": "ABC123"},
            duration_ms=47.3,
            success=True,
            log_path=log_file,
        )
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["server"] == "linc-nerve"
        assert entry["tool"] == "nerve_screenshot"
        assert entry["params"] == {"device": "ABC123"}
        assert entry["duration_ms"] == 47.3
        assert entry["success"] is True
        assert entry["error"] is None
        assert "timestamp" in entry

    def test_logs_error(self, tmp_path: Path) -> None:
        log_file = tmp_path / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-nerve",
            tool="nerve_tap",
            params={"device": "ABC123", "x": 100, "y": 200},
            duration_ms=15.0,
            success=False,
            error="DeviceNotReachable",
            log_path=log_file,
        )
        entry = json.loads(log_file.read_text().strip())
        assert entry["success"] is False
        assert entry["error"] == "DeviceNotReachable"

    def test_appends_multiple_entries(self, tmp_path: Path) -> None:
        log_file = tmp_path / "mcp-calls.jsonl"
        for i in range(3):
            log_mcp_call(
                server="linc-nerve",
                tool=f"tool_{i}",
                params={},
                duration_ms=float(i),
                success=True,
                log_path=log_file,
            )
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 3

    def test_redacts_bytes_params(self, tmp_path: Path) -> None:
        log_file = tmp_path / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-nerve",
            tool="nerve_screenshot",
            params={"data": b"\x89PNG" * 1000},
            duration_ms=10.0,
            success=True,
            log_path=log_file,
        )
        entry = json.loads(log_file.read_text().strip())
        assert entry["params"]["data"].startswith("<bytes:")

    def test_truncates_long_string_params(self, tmp_path: Path) -> None:
        log_file = tmp_path / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-nerve",
            tool="nerve_type",
            params={"text": "x" * 1000},
            duration_ms=5.0,
            success=True,
            log_path=log_file,
        )
        entry = json.loads(log_file.read_text().strip())
        assert len(entry["params"]["text"]) <= 503

    def test_rotates_at_size_limit(self, tmp_path: Path) -> None:
        log_file = tmp_path / "mcp-calls.jsonl"
        big_params = {"data": "x" * 500}
        for _ in range(50):
            log_mcp_call(
                server="test",
                tool="t",
                params=big_params,
                duration_ms=1.0,
                success=True,
                log_path=log_file,
                max_size_bytes=10_000,
            )
        assert log_file.exists()
        rotated = list(tmp_path.glob("mcp-calls.*.jsonl"))
        assert len(rotated) >= 1

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        log_file = tmp_path / "sub" / "dir" / "mcp-calls.jsonl"
        log_mcp_call(
            server="test",
            tool="t",
            params={},
            duration_ms=1.0,
            success=True,
            log_path=log_file,
        )
        assert log_file.exists()

    def test_includes_session_id_from_env(self, tmp_path: Path, monkeypatch: object) -> None:
        log_file = tmp_path / "mcp-calls.jsonl"
        monkeypatch.setenv("FORGE_RUN_ID", "run_abc123")  # type: ignore[attr-defined]
        log_mcp_call(
            server="test",
            tool="t",
            params={},
            duration_ms=1.0,
            success=True,
            log_path=log_file,
        )
        entry = json.loads(log_file.read_text().strip())
        assert entry["session_id"] == "run_abc123"

    def test_redacts_sensitive_text_param(self, tmp_path: Path) -> None:
        log_file = tmp_path / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-nerve",
            tool="nerve_type",
            params={"device": "ABC", "text": "hunter2"},
            duration_ms=1.0,
            success=True,
            log_path=log_file,
        )
        content = log_file.read_text().strip()
        assert "hunter2" not in content
        entry = json.loads(content)
        assert entry["params"]["text"] == "<redacted len=7>"
        assert entry["params"]["device"] == "ABC"

    def test_redaction_is_length_only_no_digest(self, tmp_path: Path) -> None:
        """L4: equal-length secrets redact identically — nothing reversible is logged."""
        log_file = tmp_path / "mcp-calls.jsonl"
        for text in ("same_input", "diff_input"):
            log_mcp_call(
                server="linc-nerve",
                tool="nerve_type",
                params={"text": text},
                duration_ms=1.0,
                success=True,
                log_path=log_file,
            )
        lines = [json.loads(line) for line in log_file.read_text().strip().split("\n")]
        assert lines[0]["params"]["text"] == lines[1]["params"]["text"] == "<redacted len=10>"

    def test_sensitive_key_case_insensitive_and_nested(self, tmp_path: Path) -> None:
        log_file = tmp_path / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-nerve",
            tool="nerve_type",
            params={
                "Password": "p1",
                "TOKEN": "t1",
                "extras": {"PASSWORD": "x"},
            },
            duration_ms=1.0,
            success=True,
            log_path=log_file,
        )
        content = log_file.read_text().strip()
        assert "p1" not in content
        assert "t1" not in content
        entry = json.loads(content)
        assert entry["params"]["Password"] == "<redacted len=2>"
        assert entry["params"]["TOKEN"] == "<redacted len=2>"
        assert entry["params"]["extras"]["PASSWORD"] == "<redacted len=1>"
        assert "x" not in entry["params"]["extras"]["PASSWORD"]

    def test_daemon_path_redacts_sensitive_params(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from linc_nerve.daemon.http_router import route_request

        monkeypatch.setenv("LINC_HOME", str(tmp_path))
        registry = {"nerve_type": lambda **kwargs: "typed"}
        route_request(
            method="POST",
            path="/tools/nerve_type",
            body={"device": "ABC", "text": "hunter2"},
            registry=registry,
        )
        log_file = tmp_path / "mcp-calls.jsonl"
        assert log_file.exists()
        content = log_file.read_text()
        assert "hunter2" not in content
        record = json.loads(content.strip())
        assert record["params"]["text"] == "<redacted len=7>"
        assert record["params"]["device"] == "ABC"

    def test_truncates_non_sensitive_long_string(self, tmp_path: Path) -> None:
        log_file = tmp_path / "mcp-calls.jsonl"
        long_uri = "https://example.com/" + "a" * 580
        assert len(long_uri) == 600
        log_mcp_call(
            server="linc-nerve",
            tool="nerve_navigate",
            params={"screen": long_uri},
            duration_ms=1.0,
            success=True,
            log_path=log_file,
        )
        entry = json.loads(log_file.read_text().strip())
        assert entry["params"]["screen"] == long_uri[:500] + "..."
        assert len(entry["params"]["screen"]) == 503

    def test_usage_stats_mixed_verbatim_and_redacted_records(self, tmp_path: Path) -> None:
        from linc_nerve.core.usage_stats import compute_usage_stats

        log_file = tmp_path / "mcp-calls.jsonl"
        legacy_record = {
            "timestamp": "2026-09-30T10:00:00Z",
            "server": "linc-nerve",
            "tool": "nerve_type",
            "params": {"device": "ABC", "text": "hunter2"},
            "duration_ms": 1.0,
            "success": True,
            "error": None,
            "session_id": "legacy",
        }
        log_file.write_text(json.dumps(legacy_record) + "\n")
        log_mcp_call(
            server="linc-nerve",
            tool="nerve_type",
            params={"device": "ABC", "text": "hunter2"},
            duration_ms=2.0,
            success=True,
            log_path=log_file,
        )
        stats = compute_usage_stats(log_path=log_file)
        assert stats["total_calls"] == 2

    def test_sensitive_keys_constant_and_safe_value(self) -> None:
        from linc_nerve.core.observability import (
            _SENSITIVE_PARAM_KEYS,
            _safe_params,
            _safe_value,
        )

        expected_keys = frozenset(
            {"text", "password", "passcode", "pin", "secret", "token", "otp"}
        )
        assert expected_keys == _SENSITIVE_PARAM_KEYS

        # bytes value for sensitive key
        redacted_bytes = _safe_value("secret", b"sensitive_bytes")
        assert redacted_bytes == "<redacted len=15>"

        # non-sensitive bytes
        safe_bytes = _safe_value("data", b"12345")
        assert safe_bytes == "<bytes:5>"

        # safe_params dict comprehension
        params = {"text": "secret", "device": "ABC"}
        assert _safe_params(params) == {
            "text": _safe_value("text", "secret"),
            "device": "ABC",
        }


@pytest.mark.parametrize(
    ("env_val", "expected"),
    [
        (None, "redacted"),
        ("FULL", "full"),
        ("bogus", "redacted"),
        ("off", "off"),
    ],
)
def test_usage_log_mode(
    monkeypatch: pytest.MonkeyPatch, env_val: str | None, expected: str
) -> None:
    from linc_nerve.core.observability import usage_log_mode

    if env_val is None:
        monkeypatch.delenv("NERVE_USAGE_LOG", raising=False)
    else:
        monkeypatch.setenv("NERVE_USAGE_LOG", env_val)
    assert usage_log_mode() == expected


def test_nerve_usage_log_off_no_file_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_file = tmp_path / "mcp-calls.jsonl"
    monkeypatch.setenv("NERVE_USAGE_LOG", "off")
    log_mcp_call(
        server="linc-nerve",
        tool="nerve_tap",
        params={"x": 10, "y": 20},
        duration_ms=1.0,
        success=True,
        log_path=log_file,
    )
    assert not log_file.exists()


def test_nerve_usage_log_full_raw_text_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_file = tmp_path / "mcp-calls.jsonl"
    monkeypatch.setenv("NERVE_USAGE_LOG", "full")
    log_mcp_call(
        server="linc-nerve",
        tool="nerve_type",
        params={"device": "ABC", "text": "hunter2"},
        duration_ms=1.0,
        success=True,
        log_path=log_file,
    )
    assert log_file.exists()
    entry = json.loads(log_file.read_text().strip())
    assert entry["params"]["text"] == "hunter2"


@pytest.mark.parametrize("env_val", [None, "bogus"])
def test_nerve_usage_log_unset_or_bogus_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_val: str | None
) -> None:
    log_file = tmp_path / "mcp-calls.jsonl"
    if env_val is None:
        monkeypatch.delenv("NERVE_USAGE_LOG", raising=False)
    else:
        monkeypatch.setenv("NERVE_USAGE_LOG", env_val)
    log_mcp_call(
        server="linc-nerve",
        tool="nerve_type",
        params={"device": "ABC", "text": "hunter2"},
        duration_ms=1.0,
        success=True,
        log_path=log_file,
    )
    assert log_file.exists()
    content = log_file.read_text().strip()
    assert "hunter2" not in content
    entry = json.loads(content)
    assert entry["params"]["text"] == "<redacted len=7>"


def test_linc_home_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from linc_nerve.core.observability import linc_home

    monkeypatch.setenv("LINC_HOME", str(tmp_path / "custom_linc"))
    assert linc_home() == tmp_path / "custom_linc"

    monkeypatch.delenv("LINC_HOME", raising=False)
    assert linc_home() == Path.home() / ".linc"


def test_nerve_usage_log_full_honours_secret_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_file = tmp_path / "mcp-calls.jsonl"
    monkeypatch.setenv("NERVE_USAGE_LOG", "full")
    log_mcp_call(
        server="linc-nerve",
        tool="nerve_type",
        params={"device": "ABC", "text": "hunter2", "secret": True},
        duration_ms=1.0,
        success=True,
        log_path=log_file,
    )
    assert log_file.exists()
    entry = json.loads(log_file.read_text().strip())
    assert entry["params"]["text"] == "<redacted len=7>"
    assert entry["params"]["secret"] is True
    assert entry["params"]["device"] == "ABC"


@pytest.mark.parametrize(
    "secret_val",
    [False, "true", None],
)
def test_nerve_usage_log_full_non_bool_true_secret_logs_raw(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secret_val: object
) -> None:
    log_file = tmp_path / "mcp-calls.jsonl"
    monkeypatch.setenv("NERVE_USAGE_LOG", "full")
    params: dict[str, object] = {"device": "ABC", "text": "hunter2"}
    if secret_val is not None:
        params["secret"] = secret_val
    log_mcp_call(
        server="linc-nerve",
        tool="nerve_type",
        params=params,
        duration_ms=1.0,
        success=True,
        log_path=log_file,
    )
    assert log_file.exists()
    entry = json.loads(log_file.read_text().strip())
    assert entry["params"]["text"] == "hunter2"


@pytest.mark.parametrize("mode", ["full", "redacted"])
def test_error_scrubbed_when_secret_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    log_file = tmp_path / "mcp-calls.jsonl"
    monkeypatch.setenv("NERVE_USAGE_LOG", mode)
    log_mcp_call(
        server="linc-nerve",
        tool="nerve_type",
        params={"device": "ABC", "text": "hunter2", "secret": True},
        duration_ms=1.0,
        success=False,
        error="Type failed: hunter2 rejected",
        log_path=log_file,
    )
    assert log_file.exists()
    entry = json.loads(log_file.read_text().strip())
    assert entry["error"] == "Type failed: <redacted> rejected"


@pytest.mark.parametrize("mode", ["full", "redacted"])
def test_error_not_scrubbed_when_secret_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    log_file = tmp_path / "mcp-calls.jsonl"
    monkeypatch.setenv("NERVE_USAGE_LOG", mode)
    log_mcp_call(
        server="linc-nerve",
        tool="nerve_type",
        params={"device": "ABC", "text": "hunter2", "secret": False},
        duration_ms=1.0,
        success=False,
        error="Type failed: hunter2 rejected",
        log_path=log_file,
    )
    assert log_file.exists()
    entry = json.loads(log_file.read_text().strip())
    assert entry["error"] == "Type failed: hunter2 rejected"


def test_off_mode_writes_nothing_even_with_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_file = tmp_path / "mcp-calls.jsonl"
    monkeypatch.setenv("NERVE_USAGE_LOG", "off")
    log_mcp_call(
        server="linc-nerve",
        tool="nerve_type",
        params={"device": "ABC", "text": "hunter2", "secret": True},
        duration_ms=1.0,
        success=False,
        error="Type failed: hunter2 rejected",
        log_path=log_file,
    )
    assert not log_file.exists()


def test_four_char_secret_contains_no_secret_substring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_file = tmp_path / "mcp-calls.jsonl"
    monkeypatch.setenv("NERVE_USAGE_LOG", "full")
    log_mcp_call(
        server="linc-nerve",
        tool="nerve_type",
        params={"device": "ABC", "text": "1234", "secret": True},
        duration_ms=1.0,
        success=False,
        error="Failed to enter 1234 securely",
        log_path=log_file,
    )
    assert log_file.exists()
    content = log_file.read_text()
    assert "1234" not in content
    entry = json.loads(content.strip())
    assert entry["params"]["text"] == "<redacted len=4>"
    assert entry["error"] == "Failed to enter <redacted> securely"
