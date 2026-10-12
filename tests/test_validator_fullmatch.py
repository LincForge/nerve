"""H1 regression: every allowlist validator rejects a trailing newline / CR.

2026-10-02 pre-release security review, finding H1: the allowlist regexes
were written ``^...$`` and applied with ``re.match``. In Python ``$`` also
matches just before a final ``\\n``, so ``"x\\n"`` passed every validator and
an intent-extras KEY like ``"K\\n"`` split ``am start`` into two device-shell
lines (the second one — the shell-quoted value — ran as a program).

Every test here drives a validator with the three hostile shapes the review
names and asserts (a) ``ValueError`` (or a 400 / an error dict where that is
the sink's contract) and (b) the recording fake saw NOTHING. Recording fakes
only — no device, no adb, no subprocess.
"""

from __future__ import annotations

from typing import Any

import pytest

from linc_nerve.core import adb_health
from linc_nerve.core.android import AndroidController
from linc_nerve.core.ios import IOSController
from linc_nerve.daemon.http_router import route_request
from linc_nerve.mcp import _core
from linc_nerve.mcp import device_state_tools as ds
from linc_nerve.mcp.app_tools import _nerve_launch_sync

HOSTILE = ["x\n", "x\r", "x\n;id"]


class _RecordingDevice:
    """Stands in for an adbutils device: records every shell string."""

    def __init__(self, scripted: dict[str, str] | None = None) -> None:
        self.calls: list[str] = []
        self._scripted = scripted or {}

    def shell(self, cmd: str, timeout: int = 15) -> str:
        self.calls.append(cmd)
        for needle, out in self._scripted.items():
            if needle in cmd:
                return out
        return ""


class _RecordingClient:
    def __init__(self, device: _RecordingDevice) -> None:
        self._device = device

    def device(self, serial: str) -> _RecordingDevice:
        return self._device


def _android(scripted: dict[str, str] | None = None) -> tuple[AndroidController, list[str]]:
    dev = _RecordingDevice(scripted)
    ctrl = AndroidController()
    ctrl._client = _RecordingClient(dev)
    return ctrl, dev.calls


# ---------------------------------------------------------------------------
# Every allowlist pattern is anchored \A...\Z, so even a stray ``.match``
# can't reintroduce the trailing-newline bypass.
# ---------------------------------------------------------------------------


def _allowlist_patterns() -> list[Any]:
    from linc_nerve.core import android_apps, android_input, android_ui
    from linc_nerve.daemon import http_router

    return [
        android_apps._SAFE_IDENTIFIER_RE,
        android_apps._ACTIVITY_RE,
        android_ui._LOG_TAG_RE,
        android_input._KEYCODE_RE,
        ds._PACKAGE_RE,
        IOSController._FILTER_RE,
        http_router._ROUTER_IDENTIFIER_RE,
        http_router._FILTER_ALLOWLIST_RE,
    ]


@pytest.mark.parametrize("bad", HOSTILE)
def test_every_allowlist_pattern_is_newline_proof_under_match_and_fullmatch(bad: str) -> None:
    for pattern in _allowlist_patterns():
        assert pattern.fullmatch("x"), pattern.pattern
        assert pattern.match(bad) is None, pattern.pattern
        assert pattern.fullmatch(bad) is None, pattern.pattern


# ---------------------------------------------------------------------------
# core/android_apps.py — _SAFE_IDENTIFIER_RE / _ACTIVITY_RE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", HOSTILE)
def test_kill_rejects_trailing_newline_package(bad: str) -> None:
    ctrl, calls = _android()
    with pytest.raises(ValueError):
        ctrl.kill("ABC", bad)
    assert calls == []


@pytest.mark.parametrize("bad", HOSTILE)
def test_launch_rejects_trailing_newline_package(bad: str) -> None:
    ctrl, calls = _android()
    with pytest.raises(ValueError):
        ctrl.launch("ABC", bad, activity=".Main")
    assert calls == []


@pytest.mark.parametrize("bad", HOSTILE)
def test_launch_rejects_trailing_newline_activity(bad: str) -> None:
    ctrl, calls = _android()
    with pytest.raises(ValueError):
        ctrl.launch("ABC", "com.app", activity=bad)
    assert calls == []


@pytest.mark.parametrize("bad", HOSTILE)
def test_launch_rejects_trailing_newline_extras_key(bad: str) -> None:
    ctrl, calls = _android()
    with pytest.raises(ValueError):
        ctrl.launch("ABC", "com.app", activity=".Main", extras={bad: "reboot"})
    assert calls == []


@pytest.mark.parametrize("bad", HOSTILE)
def test_grant_permission_rejects_trailing_newline_permission(bad: str) -> None:
    ctrl, calls = _android()
    with pytest.raises(ValueError):
        ctrl.grant_permission("ABC", "com.app", bad)
    assert calls == []


def test_launch_quotes_inner_class_activity_component() -> None:
    """Review L1: ``$Inner`` must reach ``am`` literally, not expand to ``""``."""
    ctrl, calls = _android()
    ctrl.launch("ABC", "com.app", activity=".Main$Inner")
    assert calls == ["am start -n 'com.app/.Main$Inner'"]


def test_format_extras_quotes_key_token() -> None:
    """Defence in depth: the key is shlex-quoted like the value (no-op on plain keys)."""
    from linc_nerve.core.android_apps import _AppsMixin

    assert _AppsMixin._format_extras({"SKIP_CONSENT": True, "N": 3, "S": "a b"}) == (
        "--ez SKIP_CONSENT true --ei N 3 --es S 'a b'"
    )


def test_resolved_launcher_component_is_quoted() -> None:
    """A device-reported inner-class launcher activity is quoted, not expanded."""
    ctrl, calls = _android(
        {"getprop": "phone", "resolve-activity": "priority=0\ncom.app/.Main$Inner\n"}
    )
    ctrl.launch("ABC", "com.app")
    assert calls[-1] == "am start -n 'com.app/.Main$Inner'"


# ---------------------------------------------------------------------------
# core/android_ui.py — _LOG_TAG_RE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", HOSTILE)
def test_logcat_rejects_trailing_newline_tag(bad: str) -> None:
    ctrl, calls = _android()
    with pytest.raises(ValueError):
        ctrl.logcat("ABC", tag=bad)
    assert calls == []


@pytest.mark.parametrize("bad", HOSTILE)
def test_android_log_stream_cmd_rejects_trailing_newline_tag(bad: str) -> None:
    ctrl, calls = _android()
    with pytest.raises(ValueError):
        ctrl.get_log_stream_cmd("ABC", tag=bad)
    assert calls == []


# ---------------------------------------------------------------------------
# core/android_input.py — _KEYCODE_RE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", HOSTILE)
def test_press_key_rejects_trailing_newline_key(bad: str) -> None:
    ctrl, calls = _android()
    with pytest.raises(ValueError):
        ctrl.press_key("ABC", bad)
    assert calls == []


# ---------------------------------------------------------------------------
# mcp/device_state_tools.py — _PACKAGE_RE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", HOSTILE)
def test_device_state_validate_package_rejects_trailing_newline(bad: str) -> None:
    assert ds._validate_package(bad) is not None


@pytest.mark.parametrize("bad", HOSTILE)
def test_device_state_tool_rejects_trailing_newline_before_shell(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctrl, calls = _android()
    monkeypatch.setattr(ds._core, "_resolve_device", lambda s: (object(), "android"))
    monkeypatch.setattr(ds._core, "_check_guard", lambda s: None)
    monkeypatch.setattr(ds._core, "_get_controller", lambda p: ctrl)
    res = ds._nerve_device_state_sync("ABC", "standby_get", package=bad)
    assert res.get("validation_error") is True
    assert calls == []


# ---------------------------------------------------------------------------
# core/ios_logs.py — _FILTER_RE
# ---------------------------------------------------------------------------

_SIM = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"


def _ios_recorder(monkeypatch: pytest.MonkeyPatch) -> tuple[IOSController, list[Any]]:
    ctrl = IOSController()
    calls: list[Any] = []

    def _record(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("validator let a hostile filter through")

    monkeypatch.setattr(ctrl, "_run", _record)
    monkeypatch.setattr(ctrl, "_run_log", _record)
    monkeypatch.setattr(ctrl, "_resolve_bundle_id", _record)
    return ctrl, calls


@pytest.mark.parametrize("bad", HOSTILE)
def test_ios_get_logs_rejects_trailing_newline_filter(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctrl, calls = _ios_recorder(monkeypatch)
    with pytest.raises(ValueError):
        ctrl.get_logs(_SIM, filter=bad)
    assert calls == []


@pytest.mark.parametrize("bad", HOSTILE)
def test_ios_log_stream_cmd_rejects_trailing_newline_filter(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctrl, calls = _ios_recorder(monkeypatch)
    with pytest.raises(ValueError):
        ctrl.get_log_stream_cmd(_SIM, filter_str=bad)
    assert calls == []


# ---------------------------------------------------------------------------
# core/adb_health.py — _NETWORK_SERIAL_RE (classifier feeding `adb connect`)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["192.0.2.1:5555\n", "192.0.2.1:5555\r", "192.0.2.1:5555\n;id"])
def test_is_network_serial_rejects_trailing_newline(bad: str) -> None:
    assert adb_health.is_network_serial(bad) is False


# ---------------------------------------------------------------------------
# daemon/http_router.py — _ROUTER_IDENTIFIER_RE / _FILTER_ALLOWLIST_RE / extras keys
# ---------------------------------------------------------------------------


def _recording_tool(calls: list[dict[str, Any]]) -> Any:
    def _tool(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"success": True}

    return _tool


@pytest.mark.parametrize("bad", HOSTILE)
@pytest.mark.parametrize(
    ("tool", "field"),
    [
        ("nerve_kill", "package"),
        ("nerve_logcat", "tag"),
        ("nerve_stream_logs", "filter_str"),
        ("nerve_stream_logs", "filter"),
    ],
)
def test_router_rejects_trailing_newline_in_guarded_field(tool: str, field: str, bad: str) -> None:
    calls: list[dict[str, Any]] = []
    resp = route_request(
        "POST", f"/tools/{tool}", {"device": "fake-1", field: bad}, {tool: _recording_tool(calls)}
    )
    assert resp["status"] == 400
    assert calls == []


@pytest.mark.parametrize("bad", [*HOSTILE, "-flag", "a b", "K;id"])
def test_router_rejects_hostile_extras_key(bad: str) -> None:
    calls: list[dict[str, Any]] = []
    resp = route_request(
        "POST",
        "/tools/nerve_launch",
        {"device": "fake-1", "package": "com.app", "extras": {bad: "reboot"}},
        {"nerve_launch": _recording_tool(calls)},
    )
    assert resp["status"] == 400
    assert calls == []


def test_router_allows_legit_extras_keys() -> None:
    calls: list[dict[str, Any]] = []
    body = {
        "device": "fake-1",
        "package": "com.app",
        "extras": {"SKIP_CONSENT": True, "com.app.EXTRA_ID": 3, "ios-arg": "x"},
    }
    resp = route_request(
        "POST", "/tools/nerve_launch", body, {"nerve_launch": _recording_tool(calls)}
    )
    assert resp["status"] == 200
    assert calls == [body]


@pytest.mark.parametrize(
    "body",
    [
        {"device": "fake-1", "package": "com.app", "extras": {"K\n": "reboot"}},
        {"device": "fake-1", "package": "", "uri": "app://x", "extras": {"K\n": "id"}},
    ],
)
def test_review_h1_end_to_end_repro_is_rejected(
    body: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's repro through the REAL tool + controller: 400, nothing on the device."""
    ctrl, calls = _android({"getprop": "phone", "resolve-activity": "com.app/.Main\n"})
    monkeypatch.setattr(_core, "_resolve_device", lambda s: (object(), "android"))
    monkeypatch.setattr(_core, "_check_guard", lambda s: None)
    monkeypatch.setattr(_core, "_get_controller", lambda p: ctrl)
    monkeypatch.setattr(_core, "_device_mgr", None)
    resp = route_request("POST", "/tools/nerve_launch", body, {"nerve_launch": _nerve_launch_sync})
    assert resp["status"] == 400
    assert calls == []
