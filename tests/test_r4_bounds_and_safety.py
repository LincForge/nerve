"""Contract & property tests for R4 bounds, daemon JSON object check, and write resilience.

Covers bead linc-nerve-hih6:
1. nerve_reserve / core/device_manager.reserve bounds on timeout_seconds (0, 86400]
   and owner_pid (0 or live pid > 1 owned by caller's uid); NaN/Inf/bool rejected;
   property check that reserve never persists a lease with expires_in_s <= 0.
2. nerve_long_press / nerve_swipe duration_ms in 0..60000 before any adb call.
3. nerve_usage_stats (since_hours in 0..87600 finite; top_n in 0..10000) and
   nerve_metrics (last_n in 0..10000).
4. HTTP daemon returns HTTP 400 JSON for non-object JSON bodies (e.g. [1,2]).
5. Refused frame write returns the tool's validation_error dict, not a raw OSError.
6. mcp/_core.py log_mcp_call catches OSError (logging one warning) so a failed
   usage-log write (e.g. planted symlink) never changes a tool's result.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import av
import pytest
from PIL import Image

from linc_nerve.core import frames, procs, usage_stats
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager, DeviceReservation
from linc_nerve.core.ios import IOSController
from linc_nerve.core.metrics import CommandMetric, MetricsCollector
from linc_nerve.daemon.http_router import route_request
from linc_nerve.mcp import _core, input_tools, sse_server
from linc_nerve.mcp.diagnostic_tools import _nerve_metrics_sync, _nerve_usage_stats_sync
from linc_nerve.mcp.reservation_tools import _nerve_reserve_sync

SERIAL = "FAKESERIAL01"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _make_stopped_mgr(db_path: Path) -> DeviceManager:
    mgr = DeviceManager(db_path=db_path)
    mgr._heartbeat_stop.set()
    mgr._heartbeat_thread.join(timeout=2)
    return mgr


def _db_rows(db_path: Path) -> list[tuple[Any, ...]]:
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        return conn.execute(
            "SELECT device_serial, timeout_seconds, owner_pid FROM reservations"
        ).fetchall()


def _write_mp4(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width, stream.height = (32, 24)
        stream.pix_fmt = "yuv420p"
        for i in range(10):
            frame = av.VideoFrame.from_image(Image.new("RGB", (32, 24), (20, 40, 60)))
            frame.pts = i
            frame.time_base = Fraction(1, 10)
            for pkt in stream.encode(frame):
                container.mux(pkt)
        for pkt in stream.encode():
            container.mux(pkt)
    return path


# ---------------------------------------------------------------------------
# 1. reserve: timeout_seconds & owner_pid bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_timeout",
    [-1, 0, 86401, 1e12, float("nan"), float("inf"), float("-inf"), True, False, 1.5, "60"],
    ids=[
        "neg_1",
        "zero",
        "over_max",
        "1e12",
        "nan",
        "inf",
        "neg_inf",
        "bool_true",
        "bool_false",
        "float_1_5",
        "str_60",
    ],
)
def test_reserve_invalid_timeout_returns_validation_error_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_timeout: Any
) -> None:
    """[contract] Invalid timeout_seconds rejected by tool and DeviceManager without DB writes."""
    db_path = tmp_path / "devices.db"
    mgr = _make_stopped_mgr(db_path)
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(_core, "_device_mgr", mgr)
    monkeypatch.setattr(_core, "_metrics", None)
    try:
        tool_res = _nerve_reserve_sync(SERIAL, timeout_seconds=bad_timeout)
        assert tool_res.get("validation_error") is True
        assert "error" in tool_res
        assert _db_rows(db_path) == []

        mgr_res = mgr.reserve(SERIAL, timeout_seconds=bad_timeout)
        assert isinstance(mgr_res, str)
        assert _db_rows(db_path) == []
    finally:
        mgr.close()


@pytest.mark.parametrize(
    "bad_pid",
    [1, -1, -99, True, False, float("nan"), float("inf"), 1.5, "123"],
    ids=["pid_1", "neg_1", "neg_99", "bool_true", "bool_false", "nan", "inf", "float_1_5", "str"],
)
def test_reserve_invalid_owner_pid_returns_validation_error_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_pid: Any
) -> None:
    """[contract] owner_pid 1, -1, non-int, NaN, Inf, bool rejected without DB writes."""
    db_path = tmp_path / "devices.db"
    mgr = _make_stopped_mgr(db_path)
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(_core, "_device_mgr", mgr)
    monkeypatch.setattr(_core, "_metrics", None)
    try:
        tool_res = _nerve_reserve_sync(SERIAL, owner_pid=bad_pid)
        assert tool_res.get("validation_error") is True
        assert "error" in tool_res
        assert _db_rows(db_path) == []

        mgr_res = mgr.reserve(SERIAL, owner_pid=bad_pid)
        assert isinstance(mgr_res, str)
        assert _db_rows(db_path) == []
    finally:
        mgr.close()


def test_reserve_rejects_dead_and_foreign_uid_owner_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dead pid or pid owned by another UID (PermissionError from os.kill) is rejected."""
    db_path = tmp_path / "devices.db"
    mgr = _make_stopped_mgr(db_path)
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(_core, "_device_mgr", mgr)
    monkeypatch.setattr(_core, "_metrics", None)
    try:
        dead = _dead_pid()
        assert _nerve_reserve_sync(SERIAL, owner_pid=dead).get("validation_error") is True
        assert isinstance(mgr.reserve(SERIAL, owner_pid=dead), str)
        assert _db_rows(db_path) == []

        def _eperm(pid: int, sig: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "kill", _eperm)
        foreign_res = _nerve_reserve_sync(SERIAL, owner_pid=4242)
        assert foreign_res.get("validation_error") is True
        assert isinstance(mgr.reserve(SERIAL, owner_pid=4242), str)
        assert _db_rows(db_path) == []
    finally:
        mgr.close()


def test_reserve_invalid_args_do_not_overwrite_existing_lease_or_create_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation runs before touching devices.db or creating a missing db."""
    monkeypatch.setenv("LINC_HOME", str(tmp_path / "fresh_home"))
    monkeypatch.setattr(_core, "_device_mgr", None)
    monkeypatch.setattr(_core, "_android", None)
    monkeypatch.setattr(_core, "_ios", None)

    bad = _nerve_reserve_sync(SERIAL, timeout_seconds=-1)
    assert bad.get("validation_error") is True
    fresh_db = tmp_path / "fresh_home" / ".local" / "share" / "linc-nerve" / "devices.db"
    assert not fresh_db.exists()

    db_path = tmp_path / "existing.db"
    mgr = _make_stopped_mgr(db_path)
    try:
        ok = mgr.reserve(SERIAL, owner="keeper", timeout_seconds=120)
        assert isinstance(ok, DeviceReservation)
        rejected = mgr.reserve(SERIAL, owner="keeper", timeout_seconds=0)
        assert isinstance(rejected, str)
        row = mgr.check(SERIAL)
        assert row is not None
        assert row.timeout_seconds == 120
    finally:
        mgr.close()


def test_reserve_accepts_valid_boundaries_and_live_own_pid(tmp_path: Path) -> None:
    """Boundary timeout_seconds 1 and 86400 and owner_pid=0 or caller's own live pid succeed."""
    mgr = _make_stopped_mgr(tmp_path / "ok.db")
    try:
        r_min = mgr.reserve("D-MIN", timeout_seconds=1, owner_pid=0)
        r_max = mgr.reserve("D-MAX", timeout_seconds=86400, owner_pid=os.getpid())
        assert isinstance(r_min, DeviceReservation)
        assert isinstance(r_max, DeviceReservation)
        assert r_min.timeout_seconds == 1
        assert r_max.timeout_seconds == 86400
    finally:
        mgr.close()


def test_property_reserve_never_persists_lease_with_non_positive_expires_in_s(
    tmp_path: Path,
) -> None:
    """Property test: reserve never persists a lease with expires_in_s <= 0."""
    rng = random.Random(20261118)
    edge_timeouts: list[Any] = [
        -100,
        -1,
        0,
        1,
        2,
        86400,
        86401,
        10**9,
        1e12,
        0.5,
        -0.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        False,
        None,
        "10",
    ]
    random_ints = [rng.randint(-5000, 100_000) for _ in range(150)]
    random_floats = [rng.uniform(-100.0, 100.0) for _ in range(50)]
    candidates: list[Any] = [*edge_timeouts, *random_ints, *random_floats]

    mgr = _make_stopped_mgr(tmp_path / "prop.db")
    try:
        for idx, val in enumerate(candidates):
            serial = f"PROP-{idx}"
            res = mgr.reserve(serial, owner=f"o-{idx}", timeout_seconds=val)
            pool_rows = {row["device_serial"]: row for row in mgr.pool()}
            if isinstance(res, DeviceReservation):
                assert serial in pool_rows
                assert pool_rows[serial]["expires_in_s"] > 0
            else:
                assert isinstance(res, str)
                assert serial not in pool_rows
    finally:
        mgr.close()


# ---------------------------------------------------------------------------
# 2. long_press / swipe: duration_ms in 0..60000 before any adb call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_duration",
    [-1, 60001, 1e9, float("nan"), float("inf"), True, False, 1.5],
    ids=["neg_1", "over_max", "1e9", "nan", "inf", "bool_true", "bool_false", "float_1_5"],
)
def test_long_press_and_swipe_reject_invalid_duration_before_adb(
    monkeypatch: pytest.MonkeyPatch, bad_duration: Any
) -> None:
    """[contract] long_press/swipe duration_ms 1e9 and -1 are rejected before any adb call."""
    mock_android = MagicMock(spec=AndroidController)
    mock_resolve = MagicMock(side_effect=AssertionError("adb/resolve must not be called"))
    monkeypatch.setattr(_core, "_resolve_device", mock_resolve)
    monkeypatch.setattr(_core, "_get_controller", lambda _p: mock_android)
    monkeypatch.setattr(_core, "_check_guard", lambda _d: None)

    lp_res = input_tools._nerve_long_press_sync(SERIAL, 100, 200, duration_ms=bad_duration)
    assert lp_res.get("validation_error") is True
    assert "duration_ms" in lp_res["error"]

    sw_res = input_tools._nerve_swipe_sync(SERIAL, 100, 200, 300, 400, duration_ms=bad_duration)
    assert sw_res.get("validation_error") is True
    assert "duration_ms" in sw_res["error"]

    sw_alias_res = input_tools._nerve_swipe_sync(
        SERIAL, x1=100, y1=200, x2=300, y2=400, duration_ms=bad_duration
    )
    assert sw_alias_res.get("validation_error") is True

    mock_resolve.assert_not_called()
    mock_android.long_press.assert_not_called()
    mock_android.swipe.assert_not_called()


@pytest.mark.parametrize("bound_ms", [0, 60000])
def test_long_press_and_swipe_accept_duration_bounds(
    monkeypatch: pytest.MonkeyPatch, bound_ms: int
) -> None:
    """Boundaries 0 and 60000 are accepted for both long_press and swipe."""
    mock_android = MagicMock(spec=AndroidController)
    monkeypatch.setattr(_core, "_resolve_device", lambda d: (object(), "android"))
    monkeypatch.setattr(_core, "_get_controller", lambda _p: mock_android)
    monkeypatch.setattr(_core, "_check_guard", lambda _d: None)

    lp_res = input_tools._nerve_long_press_sync(SERIAL, 10, 20, duration_ms=bound_ms)
    assert lp_res["success"] is True
    sw_res = input_tools._nerve_swipe_sync(SERIAL, 10, 20, 30, 40, duration_ms=bound_ms)
    assert sw_res["success"] is True


# ---------------------------------------------------------------------------
# 3. usage_stats & metrics bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_since",
    [float("nan"), float("inf"), float("-inf"), -1, -0.001, 87601, True, False, "24"],
    ids=["nan", "inf", "neg_inf", "neg_1", "neg_frac", "over_max", "bool_t", "bool_f", "str"],
)
def test_usage_stats_rejects_invalid_since_hours(bad_since: Any) -> None:
    """[contract] usage_stats since_hours NaN/inf/-1/out-of-range return validation errors."""
    res = _nerve_usage_stats_sync(since_hours=bad_since)
    assert res.get("validation_error") is True
    assert "since_hours" in res["error"]

    with pytest.raises(ValueError, match="since_hours"):
        usage_stats.compute_usage_stats(since_hours=bad_since)


@pytest.mark.parametrize(
    "bad_top_n",
    [-1, 10001, True, False, 1.5, float("nan"), "10"],
    ids=["neg_1", "over_max", "bool_true", "bool_false", "float_1_5", "nan", "str"],
)
def test_usage_stats_rejects_invalid_top_n(bad_top_n: Any) -> None:
    """[contract] usage_stats top_n -1 / >10000 / non-int return validation errors."""
    res = _nerve_usage_stats_sync(top_n=bad_top_n)
    assert res.get("validation_error") is True
    assert "top_n" in res["error"]

    with pytest.raises(ValueError, match="top_n"):
        usage_stats.compute_usage_stats(top_n=bad_top_n)


@pytest.mark.parametrize(
    "bad_last_n",
    [-1, 10001, True, False, 1.5, float("nan"), "10"],
    ids=["neg_1", "over_max", "bool_true", "bool_false", "float_1_5", "nan", "str"],
)
def test_metrics_rejects_invalid_last_n(bad_last_n: Any) -> None:
    """nerve_metrics last_n < 0 / > 10000 / non-int returns validation_error."""
    res = _nerve_metrics_sync(last_n=bad_last_n)
    assert res.get("validation_error") is True
    assert "last_n" in res["error"]


def test_usage_stats_and_metrics_valid_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary values 0..87600 for since_hours and 0..10000 for top_n/last_n succeed."""
    monkeypatch.setenv("LINC_HOME", str(tmp_path))
    assert "error" not in _nerve_usage_stats_sync(since_hours=0.0, top_n=0)
    assert "error" not in _nerve_usage_stats_sync(since_hours=87600, top_n=10000)

    mc = MetricsCollector()
    mc.record(CommandMetric(command="input", serial=SERIAL, duration_ms=5000.0, success=True))
    monkeypatch.setattr(_core, "_metrics", mc)
    res_zero = _nerve_metrics_sync(last_n=0)
    assert "error" not in res_zero
    assert res_zero["recent"] == []
    assert res_zero["slow_commands"] == []
    res_max = _nerve_metrics_sync(last_n=10000)
    assert "error" not in res_max
    assert len(res_max["recent"]) == 1


# ---------------------------------------------------------------------------
# 4. HTTP daemon non-object JSON body returns HTTP 400 JSON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_body", [b"[1,2]", b'"hello"', b"123", b"true", b"null"])
async def test_http_daemon_non_object_json_returns_400_json(raw_body: bytes) -> None:
    """[contract] POST body `[1,2]` (or any non-object JSON) gets HTTP 400 JSON, never drops."""
    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    req = (
        f"POST /tools/nerve_discover HTTP/1.1\r\n"
        f"Host: 127.0.0.1:8111\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(raw_body)}\r\n"
        f"\r\n"
    ).encode() + raw_body
    reader.feed_data(req)
    reader.feed_eof()

    await sse_server._handle_client(reader, writer)

    written = b"".join(
        c.args[0] for c in writer.write.call_args_list if c.args and isinstance(c.args[0], bytes)
    )
    assert written, "daemon dropped connection with empty response"
    head, _, body_bytes = written.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 400 "), head
    parsed = json.loads(body_bytes.decode("utf-8"))
    assert isinstance(parsed, dict) and parsed.get("error")


@pytest.mark.parametrize("bad_body", [[1, 2], "hello", 123, None])
def test_http_router_non_object_body_returns_400(bad_body: Any) -> None:
    """route_request returns 400 JSON when POST body is not a dict."""
    res = route_request("POST", "/tools/nerve_discover", bad_body, {"nerve_discover": dict})
    assert res["status"] == 400
    assert isinstance(res["body"], dict) and res["body"].get("error")


# ---------------------------------------------------------------------------
# 5. Refused frame write returns validation_error dict, not raw OSError
# ---------------------------------------------------------------------------


def test_refused_frame_write_returns_validation_error_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] Planted frame symlink refusal returns validation_error, not raw OSError."""
    vid_dir = tmp_path / "vid"
    monkeypatch.setattr(frames, "_system_tmp", lambda: vid_dir)
    video = _write_mp4(vid_dir / "clip.mp4")

    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    victim = victim_dir / "keep.txt"
    victim.write_bytes(b"untouched\n")

    out_dir = vid_dir / "out"
    out_dir.mkdir()
    (out_dir / "frame_500.png").symlink_to(victim)

    result = frames.extract_request(str(video), "0.5", str(out_dir))
    assert result.get("validation_error") is True
    assert "error" in result
    assert victim.read_bytes() == b"untouched\n"


# ---------------------------------------------------------------------------
# 6. log_mcp_call failure never changes tool result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planted_mcp_calls_symlink_does_not_fail_successful_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] When mcp-calls.jsonl is a symlink, tool still returns success."""
    canonical = Path(os.path.realpath(tmp_path))
    root = canonical / "vault"
    root.mkdir()
    victim = canonical / "victim.jsonl"
    victim.write_text("untouched\n", encoding="utf-8")
    (root / "mcp-calls.jsonl").symlink_to(victim)

    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(root))
    monkeypatch.delenv("NERVE_ENGAGEMENT", raising=False)
    monkeypatch.setenv("NERVE_USAGE_LOG", "redacted")

    # Wire fake controllers for a @device_tool call as well as a @global_tool call.
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = [
        NerveDevice(
            serial=SERIAL,
            platform=Platform.ANDROID,
            device_type=DeviceType.PHONE,
            model="Fake Phone",
            os_version="14",
        )
    ]
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    monkeypatch.setattr(_core, "_android", mock_android)
    monkeypatch.setattr(_core, "_ios", mock_ios)
    monkeypatch.setattr(_core, "_metrics", MetricsCollector())
    monkeypatch.setattr(_core, "_cached_devices", [])
    monkeypatch.setattr(_core, "_cache_timestamp", 0.0)
    monkeypatch.setattr(_core, "_device_mgr", None)
    _core._device_semaphores.clear()

    tap_tool = _core.server._tool_manager.get_tool("nerve_tap")
    stats_tool = _core.server._tool_manager.get_tool("nerve_session_stats")
    assert tap_tool is not None and stats_tool is not None

    prev_sigint = signal.getsignal(signal.SIGINT)
    try:
        with patch.object(_core.log, "warning") as mock_warn:
            stats_res = await stats_tool.fn()
            tap_res = await tap_tool.fn(device=SERIAL, x=50, y=60)
    finally:
        if _core._device_mgr is not None:
            _core._device_mgr.close()
            _core._device_mgr = None
        signal.signal(signal.SIGINT, prev_sigint)
        _core._device_semaphores.clear()

    assert "error" not in stats_res
    assert "sessions" in stats_res
    assert tap_res.get("success") is True
    assert victim.read_text(encoding="utf-8") == "untouched\n"
    assert mock_warn.call_count == 2

    # HTTP daemon dispatch (success, type-rejected 400, and raised error 400/500)
    # also survives a planted mcp-calls.jsonl symlink.
    def _typed_tool(x: int = 1) -> dict[str, Any]:
        return {"x": x}

    def _boom_tool() -> dict[str, Any]:
        raise ValueError("bad arg")

    registry = {
        "nerve_session_stats": lambda: {"sessions": {}, "count": 0},
        "nerve_typed": _typed_tool,
        "nerve_boom": _boom_tool,
    }
    with patch("linc_nerve.daemon.http_router.log.warning") as http_warn:
        http_res = route_request("POST", "/tools/nerve_session_stats", {}, registry)
        bad_type_res = route_request("POST", "/tools/nerve_typed", {"x": "nope"}, registry)
        err_res = route_request("POST", "/tools/nerve_boom", {}, registry)

    assert http_res["status"] == 200
    assert http_res["body"]["result"] == {"sessions": {}, "count": 0}
    assert bad_type_res["status"] == 400
    assert err_res["status"] == 400
    log_failures = [
        c for c in http_warn.call_args_list if c.args and c.args[0] == "mcp_call_log_failed"
    ]
    assert len(log_failures) == 3
    assert victim.read_text(encoding="utf-8") == "untouched\n"


def test_pid_alive_same_uid_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover non-int/bool, root UID check (/proc and ps fallback), and error paths."""
    assert procs.pid_alive_same_uid(True) is False  # type: ignore[arg-type]
    assert procs.pid_alive_same_uid(0) is False
    assert procs.pid_alive_same_uid(-5) is False
    assert procs.pid_alive_same_uid(os.getpid()) is True

    monkeypatch.setattr(os, "kill", lambda _p, _s: None)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getuid", lambda: 501)

    # /proc/<pid> reports foreign uid
    monkeypatch.setattr(os, "stat", lambda _path: MagicMock(st_uid=0))
    assert procs.pid_alive_same_uid(1234) is False

    # /proc/<pid> reports matching uid
    monkeypatch.setattr(os, "stat", lambda _path: MagicMock(st_uid=501))
    assert procs.pid_alive_same_uid(1234) is True

    # /proc unavailable -> ps fallback
    monkeypatch.setattr(os, "stat", MagicMock(side_effect=FileNotFoundError))
    monkeypatch.setattr(procs.subprocess, "check_output", lambda *a, **kw: "  0\n")
    assert procs.pid_alive_same_uid(1234) is False

    monkeypatch.setattr(procs.subprocess, "check_output", lambda *a, **kw: "  501\n")
    assert procs.pid_alive_same_uid(1234) is True

    monkeypatch.setattr(procs.subprocess, "check_output", MagicMock(side_effect=OSError("no ps")))
    assert procs.pid_alive_same_uid(1234) is True
