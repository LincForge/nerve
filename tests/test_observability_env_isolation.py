"""Regression: observability + DeviceManager defaults must respect LINC_HOME.

Reference: linc-reactor PR #5 (commit ac8e14f).
"""

from __future__ import annotations

from pathlib import Path

from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.observability import log_mcp_call


def test_log_mcp_call_respects_LINC_HOME(monkeypatch, tmp_path):
    monkeypatch.setenv("LINC_HOME", str(tmp_path))

    real_log = Path.home() / ".linc" / "mcp-calls.jsonl"
    pre_existed = real_log.exists()
    pre_size = real_log.stat().st_size if pre_existed else 0

    log_mcp_call(
        server="linc-nerve",
        tool="nerve_test",
        params={"smoke": True},
        duration_ms=1.0,
        success=True,
    )

    expected = tmp_path / "mcp-calls.jsonl"
    assert expected.exists(), f"log not written under LINC_HOME={tmp_path}"

    if pre_existed:
        assert real_log.stat().st_size == pre_size
    else:
        assert not real_log.exists()


def test_device_manager_respects_LINC_HOME(monkeypatch, tmp_path):
    monkeypatch.setenv("LINC_HOME", str(tmp_path))
    mgr = DeviceManager()
    try:
        real_db = Path.home() / ".local" / "share" / "linc-nerve" / "devices.db"
        assert tmp_path in mgr._db_path.parents
        assert mgr._db_path != real_db
    finally:
        mgr.close()
