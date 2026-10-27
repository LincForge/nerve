"""Lease ownership per caller (linc-nerve-jtt8, epic linc-nerve-hts0).

p0-05 (bench bug-hunt Phase 0, 2026-10-12): a case agent's plain reserve was
refused with "or pass takeover=True"; it passed --takeover with no --owner, the
lease went to the daemon's shared session id, the orchestrator's renew failed
and the agent's later plain release freed the device. These tests pin:

(a) a takeover without an explicit identity is refused and the row is intact;
(b) the conflict refusal no longer coaches takeover;
(c) owner_pid without owner defaults the owner to ``pid:<n>``;
(d) the CLI fills a default owner from ``NERVE_OWNER``;
(e) anonymous (session-id-owned) leases are flagged in the tool result;
(f) a same-owner re-reserve is reported as a renewal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from linc_cli_kit import HttpDaemonTransport, UsageError

from linc_nerve.cli.main import _with_default_owner, main
from linc_nerve.core.device_manager import (
    DeviceManager,
    DeviceReservation,
    _default_owner,
    _has_explicit_identity,
)
from linc_nerve.mcp import _core
from linc_nerve.mcp.reservation_tools import _nerve_reserve_sync


@pytest.fixture
def mgr(tmp_path: Path) -> Iterator[DeviceManager]:
    manager = DeviceManager(db_path=tmp_path / "devices.db")
    manager._heartbeat_stop.set()
    manager._heartbeat_thread.join(timeout=2)
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture
def tool_mgr(mgr: DeviceManager, monkeypatch: pytest.MonkeyPatch) -> DeviceManager:
    """Wire ``mgr`` as the tool layer's DeviceManager without touching controllers."""
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(_core, "_device_mgr", mgr)
    monkeypatch.setattr(_core, "_metrics", None)
    return mgr


# --- (a) + (b): the p0-05 regression ------------------------------------------------


class TestP005Regression:
    def test_anonymous_takeover_refused_and_lease_survives(self, mgr: DeviceManager) -> None:
        assert isinstance(mgr.reserve("D1", owner="orchestrator"), DeviceReservation)

        refused = mgr.reserve("D1", purpose="agent")
        assert isinstance(refused, str)
        assert " is held by orchestrator" in refused
        assert "takeover=True" not in refused

        anonymous = mgr.reserve("D1", takeover=True, owner_pid=0)
        assert isinstance(anonymous, str)
        assert "takeover needs an explicit owner" in anonymous
        assert " is held by " in anonymous
        lease = mgr.check("D1")
        assert lease is not None
        assert lease.owner == "orchestrator"

        assert mgr.release("D1") is False
        lease = mgr.check("D1")
        assert lease is not None
        assert lease.owner == "orchestrator"

        renewed = mgr.reserve("D1", owner="orchestrator")
        assert isinstance(renewed, DeviceReservation)
        assert renewed.owner == "orchestrator"

    def test_conflict_refusal_points_at_renew_not_takeover(self, mgr: DeviceManager) -> None:
        mgr.reserve("D1", owner="orchestrator")
        refused = mgr.reserve("D1", owner="agent")
        assert isinstance(refused, str)
        assert "If this is your own lease, re-run with the same owner to renew it." in refused
        assert "Do not take over a lease you did not create." in refused
        assert "takeover" not in refused.replace("take over", "")

    def test_explicit_owner_takeover_still_reowns(self, mgr: DeviceManager) -> None:
        mgr.reserve("D1", owner="lane-a")
        taken = mgr.reserve("D1", owner="lane-b", takeover=True)
        assert isinstance(taken, DeviceReservation)
        lease = mgr.check("D1")
        assert lease is not None
        assert lease.owner == "lane-b"

    def test_owner_pid_takeover_reowns_as_pid_owner(self, mgr: DeviceManager) -> None:
        mgr.reserve("D1", owner="lane-a")
        pid = os.getpid()
        taken = mgr.reserve("D1", owner_pid=pid, takeover=True)
        assert isinstance(taken, DeviceReservation)
        lease = mgr.check("D1")
        assert lease is not None
        assert lease.owner == f"pid:{pid}"

    def test_anonymous_takeover_on_unreserved_device_is_a_normal_reserve(
        self, mgr: DeviceManager
    ) -> None:
        result = mgr.reserve("D1", takeover=True)
        assert isinstance(result, DeviceReservation)
        assert result.owner == mgr.session_id


# --- (c): per-process default owner ---------------------------------------------------


class TestDefaultOwner:
    def test_owner_pid_without_owner_stores_pid_owner(self, mgr: DeviceManager) -> None:
        pid = os.getpid()
        result = mgr.reserve("D2", owner_pid=pid)
        assert isinstance(result, DeviceReservation)
        lease = mgr.check("D2")
        assert lease is not None
        assert lease.owner == f"pid:{pid}"
        assert mgr.release("D2") is False
        assert mgr.release("D2", owner=f"pid:{pid}") is True

    def test_neither_owner_nor_pid_stores_session_id(self, mgr: DeviceManager) -> None:
        result = mgr.reserve("D3")
        assert isinstance(result, DeviceReservation)
        lease = mgr.check("D3")
        assert lease is not None
        assert lease.owner == mgr.session_id

    @pytest.mark.parametrize(
        ("owner", "owner_pid", "expected"),
        [
            ("x", 0, "x"),
            ("x", 42, "x"),
            ("", 42, "pid:42"),
            ("", 0, "sess"),
            ("", -1, "sess"),
        ],
    )
    def test_default_owner_helper(self, owner: str, owner_pid: int, expected: str) -> None:
        assert _default_owner(owner, owner_pid, "sess") == expected

    @pytest.mark.parametrize(
        ("owner", "owner_pid", "expected"),
        [("x", 0, True), ("", 7, True), ("x", 7, True), ("", 0, False)],
    )
    def test_has_explicit_identity(self, owner: str, owner_pid: int, expected: bool) -> None:
        assert _has_explicit_identity(owner, owner_pid) is expected


# --- (d): NERVE_OWNER default on the CLI ----------------------------------------------


class TestWithDefaultOwner:
    @pytest.mark.parametrize("tool", ["nerve_reserve", "nerve_release"])
    def test_env_fills_owner(self, tool: str) -> None:
        payload = {"device": "D1"}
        out = _with_default_owner(tool, payload, {"NERVE_OWNER": "lane-x"})
        assert out == {"device": "D1", "owner": "lane-x"}
        assert payload == {"device": "D1"}  # never mutated in place

    def test_explicit_owner_wins(self) -> None:
        payload = {"device": "D1", "owner": "other"}
        out = _with_default_owner("nerve_reserve", payload, {"NERVE_OWNER": "lane-x"})
        assert out["owner"] == "other"

    def test_other_tool_untouched(self) -> None:
        payload = {"device": "D1"}
        out = _with_default_owner("nerve_tap", payload, {"NERVE_OWNER": "lane-x"})
        assert out == {"device": "D1"}

    @pytest.mark.parametrize("env", [{}, {"NERVE_OWNER": ""}])
    def test_unset_or_empty_env_changes_nothing(self, env: dict[str, str]) -> None:
        payload = {"device": "D1"}
        assert _with_default_owner("nerve_reserve", payload, env) == {"device": "D1"}

    @pytest.mark.parametrize("bad", ["a b", "x\n", "-x", "x" * 129])
    def test_invalid_env_raises_usage_error(self, bad: str) -> None:
        with pytest.raises(UsageError, match="NERVE_OWNER"):
            _with_default_owner("nerve_reserve", {"device": "D1"}, {"NERVE_OWNER": bad})

    def test_valid_grammar(self) -> None:
        good = "a" + "b" * 127
        out = _with_default_owner("nerve_release", {}, {"NERVE_OWNER": good})
        assert out["owner"] == good
        out = _with_default_owner("nerve_release", {}, {"NERVE_OWNER": "h@host:lane_1.a-b"})
        assert out["owner"] == "h@host:lane_1.a-b"


@pytest.mark.usefixtures("restore_signal_handlers")
class TestCliNerveOwner:
    def _invoke(self, args: list[str], monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []

        def fake_call(self: Any, spec: Any, payload: dict[str, Any], **_: Any) -> Any:
            sent.append(dict(payload))
            return {"success": True, "device": payload.get("device"), "owner": payload["owner"]}

        monkeypatch.setenv("NERVE_OWNER", "lane-x")
        with patch.object(HttpDaemonTransport, "call", fake_call):
            result = CliRunner().invoke(main, args)
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["success"] is True
        return sent

    def test_env_owner_is_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._invoke(["reserve", "--device", "D1"], monkeypatch)
        assert sent[-1]["owner"] == "lane-x"

    def test_explicit_owner_flag_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._invoke(["reserve", "--device", "D1", "--owner", "other"], monkeypatch)
        assert sent[-1]["owner"] == "other"


# --- (e) + (f): the tool result --------------------------------------------------------


class TestReserveToolResult:
    def test_anonymous_lease_is_flagged(self, tool_mgr: DeviceManager) -> None:
        result = _nerve_reserve_sync("D1")
        assert result["success"] is True
        assert result["shared_owner"] is True
        assert result["warning"].startswith("anonymous lease:")
        assert tool_mgr.session_id in result["warning"]
        assert "NERVE_OWNER" in result["warning"]
        # pre-existing keys keep their values
        assert result["device"] == "D1"
        assert result["session_id"] == tool_mgr.session_id
        assert result["owner"] == tool_mgr.session_id
        assert result["timeout_seconds"] == 300
        assert result["purpose"] == ""

    def test_owned_lease_is_not_flagged(self, tool_mgr: DeviceManager) -> None:
        result = _nerve_reserve_sync("D1", owner="x")
        assert result["success"] is True
        assert result["shared_owner"] is False
        assert "warning" not in result
        assert result["owner"] == "x"

    def test_renewal_is_reported(self, tool_mgr: DeviceManager) -> None:
        first = _nerve_reserve_sync("D1", owner="x", timeout_seconds=120)
        assert first["renewed"] is False
        assert first["expires_in_s"] == 120
        again = _nerve_reserve_sync("D1", owner="x", timeout_seconds=120)
        assert again["renewed"] is True
        assert again["expires_in_s"] == again["timeout_seconds"] == 120

    def test_takeover_is_not_a_renewal(self, tool_mgr: DeviceManager) -> None:
        _nerve_reserve_sync("D1", owner="a")
        taken = _nerve_reserve_sync("D1", owner="b", takeover=True)
        assert taken["renewed"] is False

    def test_anonymous_takeover_refused_through_tool(self, tool_mgr: DeviceManager) -> None:
        _nerve_reserve_sync("D1", owner="orchestrator")
        refused = _nerve_reserve_sync("D1", takeover=True)
        assert "takeover needs an explicit owner" in refused["error"]


# --- (g): daemon mode & renew_anonymous (linc-nerve-l7pm) -----------------------------


class TestRenewAnonymous:
    @staticmethod
    def _tick_and_reap(mgr: DeviceManager, serial: str, age_s: float = 61) -> float:
        ts = time.time() - age_s
        mgr._conn.execute(
            "UPDATE reservations SET last_heartbeat = ? WHERE device_serial = ?", (ts, serial)
        )
        mgr._conn.commit()
        now = time.time()
        with mgr._lock:
            mgr._renew_live_leases(now)
        mgr._reap_stale()
        return now

    def _stopped_mgr(self, tmp_path: Path, renew_anonymous: bool) -> DeviceManager:
        mgr = DeviceManager(db_path=tmp_path / "d.db", renew_anonymous=renew_anonymous)
        mgr._heartbeat_stop.set()
        mgr._heartbeat_thread.join(timeout=2)
        return mgr

    def test_renew_anonymous_false_reaps_expired_anonymous_lease(self, tmp_path: Path) -> None:
        mgr = self._stopped_mgr(tmp_path, renew_anonymous=False)
        try:
            assert isinstance(mgr.reserve("D1", timeout_seconds=60), DeviceReservation)
            self._tick_and_reap(mgr, "D1")
            assert mgr.check("D1") is None
        finally:
            mgr.close()

    def test_renew_anonymous_true_keeps_anonymous_lease(self, tmp_path: Path) -> None:
        mgr = self._stopped_mgr(tmp_path, renew_anonymous=True)
        try:
            assert isinstance(mgr.reserve("D1", timeout_seconds=60), DeviceReservation)
            now = self._tick_and_reap(mgr, "D1")
            lease = mgr.check("D1")
            assert lease is not None
            assert lease.last_heartbeat == now
        finally:
            mgr.close()

    def test_renew_anonymous_false_with_live_owner_pid_keeps_lease(self, tmp_path: Path) -> None:
        mgr = self._stopped_mgr(tmp_path, renew_anonymous=False)
        try:
            pid = os.getpid()
            assert isinstance(
                mgr.reserve("D1", timeout_seconds=60, owner_pid=pid), DeviceReservation
            )
            now = self._tick_and_reap(mgr, "D1")
            lease = mgr.check("D1")
            assert lease is not None
            assert lease.last_heartbeat == now
        finally:
            mgr.close()

    def test_renew_anonymous_false_with_dead_owner_pid_reaps(self, tmp_path: Path) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        dead_pid = proc.pid
        mgr = self._stopped_mgr(tmp_path, renew_anonymous=False)
        try:
            assert isinstance(
                mgr.reserve("D1", timeout_seconds=60, owner_pid=dead_pid), DeviceReservation
            )
            self._tick_and_reap(mgr, "D1")
            assert mgr.check("D1") is None
        finally:
            mgr.close()


class TestDaemonModeInit:
    def test_set_daemon_mode_affects_singleton_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LINC_HOME", str(tmp_path))
        for daemon_flag, expected in [(True, False), (False, True)]:
            monkeypatch.setattr(_core, "_device_mgr", None)
            monkeypatch.setattr(_core, "_daemon_mode", False)
            if daemon_flag:
                _core.set_daemon_mode()
            _core._init_singletons()
            try:
                assert _core._device_mgr is not None
                assert _core._device_mgr.renew_anonymous is expected
            finally:
                if _core._device_mgr is not None:
                    _core._device_mgr.close()
                    _core._device_mgr = None


class TestRenewalReporting:
    def test_nerve_reserve_sync_reports_renewal_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mgr_daemon = DeviceManager(db_path=tmp_path / "daemon.db", renew_anonymous=False)
        mgr_stdio = DeviceManager(db_path=tmp_path / "stdio.db", renew_anonymous=True)
        for m in (mgr_daemon, mgr_stdio):
            m._heartbeat_stop.set()
            m._heartbeat_thread.join(timeout=2)

        monkeypatch.setattr(_core, "_ensure_initialized", lambda: None)
        monkeypatch.setattr(_core, "_metrics", None)

        live_pid = os.getpid()

        try:
            monkeypatch.setattr(_core, "_device_mgr", mgr_daemon)
            res_anon_daemon = _nerve_reserve_sync("D1")
            assert res_anon_daemon["renewal"] == "caller"

            res_live_pid_daemon = _nerve_reserve_sync("D2", owner_pid=live_pid)
            assert res_live_pid_daemon["renewal"] == "daemon"

            monkeypatch.setattr(_core, "_device_mgr", mgr_stdio)
            res_anon_stdio = _nerve_reserve_sync("D3")
            assert res_anon_stdio["renewal"] == "daemon"

            res_live_pid_stdio = _nerve_reserve_sync("D4", owner_pid=live_pid)
            assert res_live_pid_stdio["renewal"] == "daemon"
        finally:
            mgr_daemon.close()
            mgr_stdio.close()
