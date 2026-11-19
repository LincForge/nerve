"""Contract tests for the PR #286 review follow-ups on NERVE_ARTIFACTS_ROOT.

Bead linc-nerve-qo86 (epic linc-nerve-3m2o). The blocker: a symlinked root
slipped past the leaf-only ``lstat`` check when spelled with a trailing
``/``, ``//`` or ``/.``. The root must now be canonical: no symlink in ANY
component (leaf or ancestor), so ``os.path.realpath(root)`` equals
``os.path.normpath(root)``. Trailing separators on a real directory are fine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog
from click.testing import CliRunner

from linc_nerve.cli import doctor as doctor_mod
from linc_nerve.core import action_log, engagement
from linc_nerve.mcp import _core
from linc_nerve.mcp.discovery_tools import _nerve_pool_sync
from linc_nerve.mcp.reservation_tools import _nerve_release_sync
from linc_nerve.mcp.screenshot_tools import _nerve_screenshot_sync


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Clean HOME / LINC_HOME / TMPDIR plus a real vault and an outside dir."""
    dirs = {name: tmp_path / name for name in ("home", "linc_home", "sys_tmp", "vault", "outside")}
    for d in dirs.values():
        d.mkdir()
    monkeypatch.setenv("HOME", str(dirs["home"]))
    monkeypatch.setenv("LINC_HOME", str(dirs["linc_home"]))
    monkeypatch.setenv("TMPDIR", str(dirs["sys_tmp"]))
    for var in ("NERVE_ARTIFACTS_ROOT", "NERVE_ENGAGEMENT", "NERVE_ACTIONLOG_ROOTS"):
        monkeypatch.delenv(var, raising=False)
    dirs["base"] = tmp_path
    return dirs


def _fake_devices(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Never reach real adb / xcrun: every controller is a mock."""
    fake_android = MagicMock()
    fake_android.screenshot.return_value = b""
    monkeypatch.setattr(_core, "_android", fake_android)
    monkeypatch.setattr(_core, "_ios", MagicMock())
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: (dev, "android"))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)
    return fake_android


def _symlinked_vault(base: Path) -> tuple[Path, Path]:
    target = base / "unencrypted"
    target.mkdir()
    link = base / "vault-link"
    link.symlink_to(target)
    return link, target


# ---------------------------------------------------------------------------
# Blocker: canonical-root contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["", "/", "//", "/.", "/./"])
def test_contract_symlink_leaf_refused_in_every_spelling(
    env: dict[str, Path], suffix: str
) -> None:
    """[contract] A symlinked root is refused however its trailing separators are spelled."""
    link, target = _symlinked_vault(env["base"])
    raw = f"{link}{suffix}"
    with pytest.raises(engagement.EngagementMisconfiguredError) as excinfo:
        engagement.artifacts_root({"NERVE_ARTIFACTS_ROOT": raw})
    # The refusal names the resolved path so the operator can see where it pointed.
    assert str(target) in str(excinfo.value)
    with pytest.raises(engagement.EngagementMisconfiguredError):
        engagement.recordings_dir({"NERVE_ARTIFACTS_ROOT": raw})
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("suffix", ["", "/"])
def test_contract_symlinked_ancestor_refused(env: dict[str, Path], suffix: str) -> None:
    """[contract] Fail closed on a symlink in an ancestor component, not just the leaf."""
    real_parent = env["base"] / "real-parent"
    (real_parent / "acme").mkdir(parents=True)
    link_parent = env["base"] / "volumes-link"
    link_parent.symlink_to(real_parent)
    raw = f"{link_parent / 'acme'}{suffix}"
    with pytest.raises(engagement.EngagementMisconfiguredError, match="symlink"):
        engagement.artifacts_root({"NERVE_ARTIFACTS_ROOT": raw})
    assert list((real_parent / "acme").iterdir()) == []


@pytest.mark.parametrize("suffix", ["/", "//", "/.", "/./"])
def test_contract_real_dir_with_trailing_separators_is_accepted(
    env: dict[str, Path], suffix: str
) -> None:
    """[contract] A real (canonical) directory is accepted and normalized."""
    vault = env["vault"]
    raw = f"{vault}{suffix}"
    root = engagement.artifacts_root({"NERVE_ARTIFACTS_ROOT": raw})
    assert root == vault
    rec = engagement.recordings_dir({"NERVE_ARTIFACTS_ROOT": raw})
    assert rec == vault / "recordings"
    assert rec.is_dir()


def test_contract_double_leading_slash_is_refused(env: dict[str, Path]) -> None:
    """[contract] POSIX keeps a leading '//' distinct; refuse it rather than guess."""
    raw = "/" + str(env["vault"])
    with pytest.raises(engagement.EngagementMisconfiguredError):
        engagement.artifacts_root({"NERVE_ARTIFACTS_ROOT": raw})


def test_contract_symlink_root_with_trailing_slash_refuses_tool(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] End to end: the review repro no longer writes into the link target."""
    fake_android = _fake_devices(monkeypatch)
    link, target = _symlinked_vault(env["base"])
    monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-01")
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", f"{link}/")
    res = _nerve_screenshot_sync("DEV1")
    fake_android.screenshot.assert_not_called()
    assert res.get("error_category") == "engagement_misconfigured"
    assert list(target.iterdir()) == []


# ---------------------------------------------------------------------------
# Non-blocking: release / pool stay usable on a misconfigured engagement
# ---------------------------------------------------------------------------


def _misconfigure(monkeypatch: pytest.MonkeyPatch, env: dict[str, Path]) -> None:
    monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-01")
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(env["base"] / "unmounted-vault"))


def test_contract_release_and_pool_exempt_when_session_live(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] A vault that unmounts mid-session must not strand a reservation."""
    _fake_devices(monkeypatch)
    _misconfigure(monkeypatch, env)
    mgr = MagicMock()
    mgr.release.return_value = True
    mgr.pool.return_value = []
    mgr.session_id = "sess-1"
    monkeypatch.setattr(_core, "_device_mgr", mgr)
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: None)

    assert _nerve_release_sync("DEV1") == {"success": True, "device": "DEV1"}
    pool = _nerve_pool_sync()
    assert pool.get("error_category") is None
    assert pool["count"] == 0
    # Artifact writers are still refused.
    assert _nerve_screenshot_sync("DEV1").get("error_category") == "engagement_misconfigured"
    for key in ("home", "linc_home", "sys_tmp"):
        assert list(env[key].iterdir()) == []


def test_contract_release_and_pool_still_refused_before_init(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] A misconfigured process that never initialised must not create devices.db."""
    _fake_devices(monkeypatch)
    _misconfigure(monkeypatch, env)
    monkeypatch.setattr(_core, "_device_mgr", None)
    init = MagicMock()
    monkeypatch.setattr(_core, "_ensure_initialized", init)

    assert _nerve_release_sync("DEV1").get("error_category") == "engagement_misconfigured"
    assert _nerve_pool_sync().get("error_category") == "engagement_misconfigured"
    init.assert_not_called()
    for key in ("home", "linc_home", "sys_tmp"):
        assert list(env[key].iterdir()) == []


# ---------------------------------------------------------------------------
# Non-blocking: nerve doctor reports a bad root as one failed check
# ---------------------------------------------------------------------------


def _stub_doctor_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod,
        "_run_adb_probe",
        lambda *_a, **_k: (True, "List of devices attached\n"),
    )
    monkeypatch.setattr(doctor_mod, "_probe_daemon_health", lambda _port: None)
    monkeypatch.setattr(doctor_mod, "_probe_xcode_clt", lambda: None)


def test_contract_doctor_reports_bad_root_as_failed_check(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] doctor exits 1 with an artifacts-root fail, not a traceback."""
    link, target = _symlinked_vault(env["base"])
    monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-01")
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", f"{link}/")
    _stub_doctor_probes(monkeypatch)
    executed: list[object] = []
    monkeypatch.setattr(doctor_mod, "_execute_fix", executed.append)

    result = CliRunner().invoke(doctor_mod.doctor, ["--json", "--fix"])
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    payload = json.loads(result.output)
    checks = {c["name"]: c for c in payload["checks"]}
    assert checks["artifacts-root"]["status"] == "fail"
    assert "symlink" in checks["artifacts-root"]["detail"]
    assert checks["artifacts-root"]["hint"]
    # No mkdir fix is planned (or run) while the root is misconfigured.
    assert all(f["kind"] != "mkdir" for f in payload["fixes"])
    assert all(getattr(f, "kind", "") != "mkdir" for f in executed)
    assert list(target.iterdir()) == []


def test_doctor_reports_valid_root_ok_and_omits_check_when_unset(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid root is an ok check; with no root the check is absent (count unchanged)."""
    _stub_doctor_probes(monkeypatch)
    probes = doctor_mod.gather_probes()
    assert "artifacts-root" not in {r.name for r in doctor_mod.run_checks(probes)}

    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(env["vault"]))
    probes = doctor_mod.gather_probes()
    by_name = {r.name: r for r in doctor_mod.run_checks(probes)}
    assert by_name["artifacts-root"].status == "ok"
    assert str(env["vault"]) in by_name["artifacts-root"].detail


# ---------------------------------------------------------------------------
# Non-blocking: NERVE_ACTIONLOG_ROOTS entries outside the root are reported
# ---------------------------------------------------------------------------


def test_actionlog_roots_outside_artifacts_root_logged_not_silent(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropped NERVE_ACTIONLOG_ROOTS entries emit a structlog warning naming them."""
    inside = env["vault"] / "actionlogs" / "team"
    inside.mkdir(parents=True)
    environ = {
        "NERVE_ARTIFACTS_ROOT": str(env["vault"]),
        "NERVE_ACTIONLOG_ROOTS": os.pathsep.join([str(env["outside"]), str(inside)]),
    }
    with structlog.testing.capture_logs() as logs:
        roots = action_log.actionlog_roots(environ)
    assert roots == [inside]
    dropped = [e for e in logs if e["event"] == "actionlog_root_outside_artifacts_root"]
    assert len(dropped) == 1
    assert dropped[0]["dropped"] == [str(env["outside"])]
    assert dropped[0]["log_level"] == "warning"
