"""Regression: single-shot `nerve record-fixed` falls back to LocalTransport on daemon outage."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

import linc_nerve.cli.main as cli_main


class _FailingClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FailingClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, *args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("daemon unreachable")


def test_record_fixed_falls_back_to_local_transport_when_daemon_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] nerve record-fixed is self-contained and works via in-process fallback."""
    calls: list[tuple[str, int]] = []

    def fake_record_fixed(*, device: str, duration_seconds: int = 15) -> dict[str, Any]:
        calls.append((device, duration_seconds))
        return {"status": "success", "file_path": "/canonical/root/recordings/clip.mp4"}

    monkeypatch.setattr(cli_main.TRANSPORT.fallback, "_before", lambda: None)
    monkeypatch.setattr(
        cli_main.TRANSPORT.fallback,
        "_fn_for",
        lambda spec: fake_record_fixed,
    )
    monkeypatch.setattr(cli_main.TRANSPORT, "_client_factory", _FailingClient)

    res = CliRunner().invoke(
        cli_main.main,
        ["record-fixed", "--device", "DEV1", "--duration-seconds", "5"],
    )

    assert res.exit_code == 0, res.stderr
    assert calls == [("DEV1", 5)]
    payload = json.loads(res.stdout)
    assert payload == {"status": "success", "file_path": "/canonical/root/recordings/clip.mp4"}


def test_record_start_remains_daemon_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """[pin] Multi-call nerve_record_start still requires a running daemon."""
    monkeypatch.setattr(cli_main.TRANSPORT, "_client_factory", _FailingClient)

    res = CliRunner().invoke(cli_main.main, ["record-start", "--device", "DEV1"])

    assert res.exit_code == 1
    envelope = json.loads(res.stderr)["error"]
    assert envelope["code"] == "DAEMON_UNAVAILABLE"


def test_daemon_only_set_excludes_record_fixed_and_keeps_record_start() -> None:
    """[contract] _DAEMON_ONLY drops single-shot record_fixed but keeps multi-call record_start."""
    assert "nerve_record_fixed" not in cli_main._DAEMON_ONLY
    assert "nerve_record_start" in cli_main._DAEMON_ONLY
    assert "nerve_record_stop" in cli_main._DAEMON_ONLY
