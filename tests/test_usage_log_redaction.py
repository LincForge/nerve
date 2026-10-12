"""L4 regression: usage-log redaction is length-only and covers extras/uri/lists.

Security review 2026-10-02 (linc-nerve 0.1.0 pre-release), finding L4: the
``sha256[:8]`` redaction of a 4-digit ``text``/``pin`` is brute-forced
instantly; ``extras`` values, ``uri`` query tokens and dicts nested inside
lists were logged verbatim; the log file was created 0644.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from linc_nerve.core.observability import log_mcp_call


def _log_one(log_file: Path, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    log_mcp_call(
        server="linc-nerve",
        tool="nerve_launch",
        params=params,
        duration_ms=1.0,
        success=True,
        log_path=log_file,
    )
    content = log_file.read_text().strip()
    return content, json.loads(content)["params"]


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NERVE_USAGE_LOG", raising=False)


def test_short_secret_logged_as_length_only_without_hash(tmp_path: Path) -> None:
    content, params = _log_one(tmp_path / "mcp-calls.jsonl", {"text": "4821"})
    assert params["text"] == "<redacted len=4>"
    assert "sha256" not in content
    assert "4821" not in content


def test_extras_values_absent_in_default_mode(tmp_path: Path) -> None:
    content, params = _log_one(
        tmp_path / "mcp-calls.jsonl",
        {"package": "com.example", "extras": {"AUTH_TOKEN": "x", "SESSION": "s3cr3t-value"}},
    )
    assert params["extras"] == {
        "AUTH_TOKEN": "<redacted len=1>",
        "SESSION": "<redacted len=12>",
    }
    assert "s3cr3t-value" not in content
    assert params["package"] == "com.example"


def test_uri_value_absent_in_default_mode(tmp_path: Path) -> None:
    content, params = _log_one(tmp_path / "mcp-calls.jsonl", {"uri": "app://x?token=y"})
    assert params["uri"] == "<redacted len=15>"
    assert "token=y" not in content
    assert "app://" not in content


def test_dicts_nested_in_lists_are_redacted(tmp_path: Path) -> None:
    content, params = _log_one(
        tmp_path / "mcp-calls.jsonl",
        {"steps": [{"password": "hunter2", "label": "ok"}, "plain"]},
    )
    assert "hunter2" not in content
    assert params["steps"] == [{"password": "<redacted len=7>", "label": "ok"}, "plain"]


def test_sensitive_non_string_value_is_redacted(tmp_path: Path) -> None:
    content, params = _log_one(tmp_path / "mcp-calls.jsonl", {"pin": 4821})
    assert params["pin"] == "<redacted len=4>"
    assert "4821" not in content


def test_log_file_created_mode_0600(tmp_path: Path) -> None:
    log_file = tmp_path / "mcp-calls.jsonl"
    _log_one(log_file, {"device": "ABC"})
    assert stat.S_IMODE(os.stat(log_file).st_mode) == 0o600


def test_existing_world_readable_log_tightened_to_0600(tmp_path: Path) -> None:
    log_file = tmp_path / "mcp-calls.jsonl"
    log_file.write_text("")
    log_file.chmod(0o644)
    _log_one(log_file, {"device": "ABC"})
    assert stat.S_IMODE(os.stat(log_file).st_mode) == 0o600


def test_full_mode_still_logs_extras_and_uri_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NERVE_USAGE_LOG", "full")
    _, params = _log_one(
        tmp_path / "mcp-calls.jsonl",
        {"uri": "app://x?token=y", "extras": {"AUTH_TOKEN": "x"}},
    )
    assert params == {"uri": "app://x?token=y", "extras": {"AUTH_TOKEN": "x"}}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abc", "<redacted len=3>"),
        (b"abcd", "<redacted len=4>"),
        (12, "<redacted len=2>"),
        (None, "<redacted len=4>"),
        (
            {"k": "vv", "n": ["a", {"d": "eee"}]},
            {"k": "<redacted len=2>", "n": ["<redacted len=1>", {"d": "<redacted len=3>"}]},
        ),
        (("ab", "c"), ["<redacted len=2>", "<redacted len=1>"]),
    ],
)
def test_redact_all_preserves_shape_and_hides_every_leaf(value: Any, expected: Any) -> None:
    from linc_nerve.core.observability import _redact_all

    assert _redact_all(value) == expected
