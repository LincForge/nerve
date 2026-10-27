"""Tests for nerve-output retention cap, stale-dir sweep, and `nerve cleanup` (linc-nerve-si3y)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

import pytest
import structlog
import structlog.testing
from click.testing import CliRunner

from linc_nerve.cli.main import main
from linc_nerve.core import output_retention as retention
from linc_nerve.mcp import _core

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")


def _set_mtime_tree(path: Path, mtime: float) -> None:
    """Set atime/mtime on *path* and every non-symlink child inside it."""
    if path.is_dir() and not path.is_symlink():
        for child in path.iterdir():
            if not child.is_symlink():
                os.utime(child, (mtime, mtime))
    os.utime(path, (mtime, mtime), follow_symlinks=False)


def test_retention_cap_205_outputs_leaves_200_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[test] 205 outputs with NERVE_OUTPUT_KEEP=200 leave exactly 200 files."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(_core, "_OUTPUT_DIR", None)
    monkeypatch.setattr(_core, "_file_counter", 0)
    monkeypatch.setenv("NERVE_OUTPUT_KEEP", "200")

    paths: list[Path] = []
    for i in range(205):
        p = Path(_core._next_output_path("emu-1", "screenshot"))
        p.write_bytes(f"frame-{i}".encode())
        paths.append(p)

    out_dir = Path(_core._output_dir())
    remaining = sorted(p.name for p in out_dir.iterdir() if p.name != retention.LOCK_NAME)
    assert len(remaining) == 200
    assert paths[-1].exists()
    assert paths[-1].name == "emu-1_0205_screenshot.png"
    assert not paths[0].exists()
    assert not paths[4].exists()
    assert paths[5].exists()


def test_retention_cap_preserves_non_matching_files_and_symlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only regular files matching <serial>_<NNNN>_<tool>.<ext> are pruned."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(_core, "_OUTPUT_DIR", None)
    monkeypatch.setattr(_core, "_file_counter", 0)
    monkeypatch.setenv("NERVE_OUTPUT_KEEP", "3")

    out_dir = Path(_core._output_dir())
    note = out_dir / "README.txt"
    note.write_text("keep")
    victim = tmp_path / "outside.png"
    victim.write_bytes(b"keep-outside")
    sym = out_dir / "emu-1_0000_screenshot.png"
    sym.symlink_to(victim)

    for _ in range(5):
        p = Path(_core._next_output_path("emu-1", "screenshot"))
        p.write_bytes(b"png")

    matching = sorted(
        p.name
        for p in out_dir.iterdir()
        if p.is_file() and not p.is_symlink() and p != note and p.name != retention.LOCK_NAME
    )
    assert len(matching) == 3
    assert note.exists()
    assert sym.is_symlink()
    assert victim.read_bytes() == b"keep-outside"


def test_prune_output_dir_tiebreaks_by_mtime_and_normalizes_invalid_keep(
    tmp_path: Path,
) -> None:
    """When two files have the same counter, the older mtime is pruned first; keep<=0 -> 200."""
    f_newer = tmp_path / "alpha_0001_screenshot.png"
    f_older = tmp_path / "zeta_0001_screenshot.png"
    f_newer.write_bytes(b"newer")
    f_older.write_bytes(b"older")
    now = time.time()
    os.utime(f_older, (now - 100, now - 100))
    os.utime(f_newer, (now, now))

    assert _core._prune_output_dir(tmp_path, keep=0) == []
    removed = _core._prune_output_dir(tmp_path, keep=2)
    assert removed == [str(f_older)]
    assert not f_older.exists()
    assert f_newer.exists()


@pytest.mark.parametrize("raw_val", ["0", "-10", "not-an-int", ""])
def test_retention_cap_invalid_or_zero_env_uses_default(
    monkeypatch: pytest.MonkeyPatch, raw_val: str
) -> None:
    """0 or invalid NERVE_OUTPUT_KEEP / NERVE_OUTPUT_MAX_AGE_S falls back to defaults."""
    monkeypatch.setenv("NERVE_OUTPUT_KEEP", raw_val)
    monkeypatch.setenv("NERVE_OUTPUT_MAX_AGE_S", raw_val)
    assert _core._output_keep() == 200
    assert _core._output_max_age_s() == 86400


def test_stale_sweep_removes_old_dir_and_preserves_fresh_and_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[test] Stale-dir sweep removes a 2-day-old nerve-output-* dir on first _output_dir()
    call and leaves a fresh dir, a symlinked dir and its target untouched."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(_core, "_OUTPUT_DIR", None)
    monkeypatch.setenv("NERVE_OUTPUT_SWEEP", "1")
    monkeypatch.delenv("NERVE_OUTPUT_MAX_AGE_S", raising=False)

    now = time.time()
    two_days_ago = now - 2 * 86400

    stale_dir = tmp_path / "nerve-output-stale"
    stale_dir.mkdir(mode=0o700)
    (stale_dir / "emu_0001_screenshot.png").write_bytes(b"old-shot")
    _set_mtime_tree(stale_dir, two_days_ago)

    # Dir with old dir mtime but a fresh file inside must be preserved:
    fresh_entry_dir = tmp_path / "nerve-output-fresh-child"
    fresh_entry_dir.mkdir(mode=0o700)
    fresh_child = fresh_entry_dir / "emu_0001_screenshot.png"
    fresh_child.write_bytes(b"new-shot")
    os.utime(fresh_entry_dir, (two_days_ago, two_days_ago))
    os.utime(fresh_child, (now, now))

    victim_dir = tmp_path / "victim-dir"
    victim_dir.mkdir()
    victim_file = victim_dir / "precious.txt"
    victim_file.write_text("do not delete")
    _set_mtime_tree(victim_dir, two_days_ago)

    symlink_dir = tmp_path / "nerve-output-x"
    symlink_dir.symlink_to(victim_dir)

    other_old = tmp_path / "unrelated-old"
    other_old.mkdir()
    _set_mtime_tree(other_old, two_days_ago)

    created = Path(_core._output_dir())

    assert created.exists()
    assert not stale_dir.exists()
    assert fresh_entry_dir.exists()
    assert fresh_child.exists()
    assert symlink_dir.is_symlink()
    assert victim_dir.exists()
    assert victim_file.read_text() == "do not delete"
    assert other_old.exists()

    # Even if own _OUTPUT_DIR has an old mtime, _sweep_stale_output_dirs never deletes it:
    _set_mtime_tree(created, two_days_ago)
    assert _core._sweep_stale_output_dirs(tmp_path) == []
    assert created.exists()

    # Subsequent _output_dir() calls in the same process do not re-sweep:
    late_stale = tmp_path / "nerve-output-late"
    late_stale.mkdir(mode=0o700)
    _set_mtime_tree(late_stale, two_days_ago)
    assert Path(_core._output_dir()) == created
    assert late_stale.exists()


def test_stale_sweep_skips_dir_owned_by_another_uid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A nerve-output-* dir owned by another uid is left untouched."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(_core, "_OUTPUT_DIR", None)
    monkeypatch.setenv("NERVE_OUTPUT_SWEEP", "1")

    two_days_ago = time.time() - 2 * 86400
    foreign_dir = tmp_path / "nerve-output-foreign"
    foreign_dir.mkdir(mode=0o700)
    (foreign_dir / "emu_0001_screenshot.png").write_bytes(b"foreign")
    _set_mtime_tree(foreign_dir, two_days_ago)

    monkeypatch.setattr(os, "getuid", lambda: os.stat(foreign_dir).st_uid + 1)

    _core._output_dir()
    assert foreign_dir.exists()


def test_stale_sweep_swallows_and_logs_errors_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sweep errors are swallowed and logged once (one structlog event), never on stdout."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(_core, "_OUTPUT_DIR", None)
    monkeypatch.setenv("NERVE_OUTPUT_SWEEP", "1")

    two_days_ago = time.time() - 2 * 86400
    for name in ("nerve-output-err1", "nerve-output-err2"):
        d = tmp_path / name
        d.mkdir(mode=0o700)
        _set_mtime_tree(d, two_days_ago)

    def _boom(_path: str) -> None:
        raise PermissionError("simulated rmtree failure")

    monkeypatch.setattr(shutil, "rmtree", _boom)

    with structlog.testing.capture_logs() as logs:
        out = _core._output_dir()
    assert Path(out).exists()
    events = [e for e in logs if e["event"] == "stale_output_dir_sweep_error"]
    assert len(events) == 1
    assert events[0]["count"] == 2
    assert capsys.readouterr().out == ""


def test_nerve_cleanup_removes_stale_and_idle_dirs_and_prints_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[test] `nerve cleanup` removes stale + idle dirs and prints the removed list."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    now = time.time()
    two_days_ago = now - 2 * 86400
    two_mins_ago = now - 120

    stale_dir = tmp_path / "nerve-output-stale"
    stale_dir.mkdir(mode=0o700)
    (stale_dir / "emu_0001_screenshot.png").write_bytes(b"123456789012")
    (stale_dir / "subdir").mkdir()
    _set_mtime_tree(stale_dir, two_days_ago)

    idle_dir = tmp_path / "nerve-output-idle"
    idle_dir.mkdir(mode=0o700)
    (idle_dir / "emu_0002_screenshot.png").write_bytes(b"12345678")
    _set_mtime_tree(idle_dir, two_mins_ago)

    active_dir = tmp_path / "nerve-output-active"
    active_dir.mkdir(mode=0o700)
    (active_dir / "emu_0003_screenshot.png").write_bytes(b"fresh")

    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    (victim_dir / "data.txt").write_text("safe")
    _set_mtime_tree(victim_dir, two_days_ago)

    symlink_dir = tmp_path / "nerve-output-symlink"
    symlink_dir.symlink_to(victim_dir)

    unrelated_dir = tmp_path / "other-temp-dir"
    unrelated_dir.mkdir()
    _set_mtime_tree(unrelated_dir, two_days_ago)

    result = CliRunner().invoke(main, ["cleanup"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert payload["removed"] == sorted([str(stale_dir), str(idle_dir)])
    assert payload["skipped"] == sorted([str(active_dir), str(symlink_dir)])
    assert payload["bytes_freed"] == 20

    assert not stale_dir.exists()
    assert not idle_dir.exists()
    assert active_dir.exists()
    assert symlink_dir.is_symlink()
    assert victim_dir.exists()
    assert (victim_dir / "data.txt").read_text() == "safe"
    assert unrelated_dir.exists()


def test_nerve_cleanup_all_and_human_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--all` also removes <60s active dirs; `--human` renders readable text."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    active_dir = tmp_path / "nerve-output-active"
    active_dir.mkdir(mode=0o700)
    (active_dir / "emu_0001_screenshot.png").write_bytes(b"abcd")
    sym_dir = tmp_path / "nerve-output-sym"
    sym_dir.symlink_to(tmp_path)

    result = CliRunner().invoke(main, ["cleanup", "--all", "--human"])
    assert result.exit_code == 0, result.output
    assert not active_dir.exists()
    assert f"removed: {active_dir}" in result.stdout
    assert f"skipped: {sym_dir}" in result.stdout
    assert "freed 4 bytes" in result.stdout


def test_nerve_cleanup_skips_foreign_uid_and_rmtree_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`nerve cleanup` skips directories owned by another uid or failing rmtree."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    two_days_ago = time.time() - 2 * 86400
    foreign_dir = tmp_path / "nerve-output-foreign"
    foreign_dir.mkdir(mode=0o700)
    _set_mtime_tree(foreign_dir, two_days_ago)

    assert retention.is_owned_output_dir(str(tmp_path / "nerve-output-missing"), 0) is None

    monkeypatch.setattr(os, "getuid", lambda: os.stat(foreign_dir).st_uid + 1)
    result = CliRunner().invoke(main, ["cleanup", "--all"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {"removed": [], "skipped": [str(foreign_dir)], "bytes_freed": 0}
    assert foreign_dir.exists()

    # Now restore uid and simulate rmtree OSError:
    monkeypatch.undo()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(
        shutil, "rmtree", lambda _p: (_ for _ in ()).throw(OSError("rmtree denied"))
    )
    result2 = CliRunner().invoke(main, ["cleanup", "--all"])
    assert result2.exit_code == 0, result2.output
    assert json.loads(result2.stdout) == {
        "removed": [],
        "skipped": [str(foreign_dir)],
        "bytes_freed": 0,
    }


# ---------------------------------------------------------------------------
# Liveness: a dir whose owner process is alive is never removed (review of PR #240)
# ---------------------------------------------------------------------------


def _old_output_dir(root: Path, name: str, age_s: float = 3 * 86400) -> Path:
    """A nerve-output-* dir whose dir mtime and entries are *age_s* old."""
    d = root / name
    d.mkdir(mode=0o700)
    (d / "emu_0001_screenshot.png").write_bytes(b"shot")
    _set_mtime_tree(d, time.time() - age_s)
    return d


def _hold_lock(d: Path) -> BinaryIO:
    """Hold the owner lock on *d* exactly as a live nerve process does."""
    handle = retention.acquire_dir_lock(str(d))
    assert handle is not None
    _set_mtime_tree(d, time.time() - 3 * 86400)
    return handle


@pytest.fixture
def isolated_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """tmp_path as the temp root, a fresh _core output dir state, sweep opted in."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(_core, "_OUTPUT_DIR", None)
    monkeypatch.setenv("NERVE_OUTPUT_SWEEP", "1")
    return tmp_path


def test_contract_live_locked_dir_survives_nerve_cleanup_all(isolated_tmp: Path) -> None:
    """[contract] A lock-held dir older than both thresholds survives `nerve cleanup --all`."""
    live = _old_output_dir(isolated_tmp, "nerve-output-live")
    lock = _hold_lock(live)
    dead = _old_output_dir(isolated_tmp, "nerve-output-dead")
    try:
        result = CliRunner().invoke(main, ["cleanup", "--all"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["removed"] == [str(dead)]
        assert str(live) in payload["skipped"]
        assert (live / "emu_0001_screenshot.png").exists()
    finally:
        lock.close()


def test_contract_live_locked_dir_survives_first_output_sweep(isolated_tmp: Path) -> None:
    """[contract] The first-output sweep keeps a live-locked dir older than max-age."""
    live = _old_output_dir(isolated_tmp, "nerve-output-live")
    lock = _hold_lock(live)
    dead = _old_output_dir(isolated_tmp, "nerve-output-dead")
    try:
        _core._output_dir()
        assert live.exists()
        assert (live / "emu_0001_screenshot.png").exists()
        assert not dead.exists()
    finally:
        lock.close()


def test_contract_output_dir_holds_its_own_lock(isolated_tmp: Path) -> None:
    """[contract] The process's own output dir reads as live to every other process."""
    own = _core._output_dir()
    assert retention.dir_is_live(own) is True
    assert (Path(own) / retention.LOCK_NAME).read_text().strip() == str(os.getpid())


def test_contract_output_dir_recreated_after_deletion(isolated_tmp: Path) -> None:
    """[contract] _output_dir() recreates a deleted dir; the next spill path is writable."""
    first = _core._output_dir()
    shutil.rmtree(first)
    second = _core._output_dir()
    assert Path(second).is_dir()
    assert Path(second).parent == isolated_tmp
    assert stat.S_IMODE(os.stat(second).st_mode) == 0o700
    assert retention.dir_is_live(second) is True
    path = Path(_core._next_output_path("emu-1", "screenshot"))
    path.write_bytes(b"png")
    assert path.parent == Path(second)


def test_contract_output_dir_replaced_when_swapped_for_symlink(isolated_tmp: Path) -> None:
    """[contract] A deleted own dir re-created as a symlink is never reused (hostile)."""
    first = _core._output_dir()
    victim = isolated_tmp / "victim"
    victim.mkdir()
    shutil.rmtree(first)
    Path(first).symlink_to(victim)
    second = _core._output_dir()
    assert second != first
    assert not Path(second).is_symlink()
    assert list(victim.iterdir()) == []


@pytest.mark.parametrize("content", [b"", b"not-a-pid\n", b"\x00\xff garbage", b"999999999\n"])
def test_contract_unlocked_dir_with_garbage_or_dead_pid_is_stale(
    isolated_tmp: Path, content: bytes
) -> None:
    """[contract] An unheld lock file (garbage or dead-pid contents) does not keep a dir."""
    d = _old_output_dir(isolated_tmp, "nerve-output-orphan")
    (d / retention.LOCK_NAME).write_bytes(content)
    _set_mtime_tree(d, time.time() - 3 * 86400)
    assert retention.dir_is_live(str(d)) is False
    _core._output_dir()
    assert not d.exists()


def test_contract_legacy_dir_without_lock_file_is_not_live(tmp_path: Path) -> None:
    """[contract] A dir from an older nerve (no lock file) falls back to the age rules."""
    d = _old_output_dir(tmp_path, "nerve-output-legacy")
    assert retention.dir_is_live(str(d)) is False


def test_contract_symlinked_lock_file_fails_safe_as_live(tmp_path: Path) -> None:
    """[contract] A symlinked lock file is unknown state: the dir is treated as live (hostile)."""
    d = _old_output_dir(tmp_path, "nerve-output-symlock")
    target = tmp_path / "elsewhere.lock"
    target.write_text("1")
    (d / retention.LOCK_NAME).symlink_to(target)
    assert retention.dir_is_live(str(d)) is True
    out = retention.cleanup_output_dirs(tmp_path, remove_all=True)
    assert out["removed"] == []
    assert d.exists()
    assert target.exists()


def test_contract_lock_file_that_is_a_directory_fails_safe_as_live(tmp_path: Path) -> None:
    """[contract] A lock path that cannot be opened as a file reads as live."""
    d = _old_output_dir(tmp_path, "nerve-output-lockdir")
    (d / retention.LOCK_NAME).mkdir()
    assert retention.dir_is_live(str(d)) is True


def test_contract_flock_error_fails_safe_as_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[contract] An unexpected flock error is unknown state: treated as live, never removed."""
    d = _old_output_dir(tmp_path, "nerve-output-enolck")
    (d / retention.LOCK_NAME).write_text("1")

    def _enolck(_fd: int, _op: int) -> None:
        raise OSError(37, "No locks available")

    monkeypatch.setattr(retention.fcntl, "flock", _enolck)
    assert retention.dir_is_live(str(d)) is True
    assert retention.sweep_stale_output_dirs(tmp_path, None, 60, time.time()) == []
    assert d.exists()


def test_contract_acquire_dir_lock_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[contract] A failed lock acquisition never raises: the output dir still works."""

    def _enolck(_fd: int, _op: int) -> None:
        raise OSError(37, "No locks available")

    monkeypatch.setattr(retention.fcntl, "flock", _enolck)
    assert retention.acquire_dir_lock(str(tmp_path)) is None
    assert retention.acquire_dir_lock(str(tmp_path / "missing")) is None


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, True),
        ({"NERVE_OUTPUT_SWEEP": "0"}, False),
        ({"PYTEST_CURRENT_TEST": "t"}, False),
        ({"PYTEST_CURRENT_TEST": "t", "NERVE_OUTPUT_SWEEP": "1"}, True),
        ({"NERVE_OUTPUT_SWEEP": "garbage"}, True),
    ],
)
def test_contract_sweep_enabled(env: dict[str, str], expected: bool) -> None:
    """[contract] Sweep is off under pytest unless opted in; NERVE_OUTPUT_SWEEP=0 disables it."""
    assert retention.sweep_enabled(env) is expected


def test_first_output_sweep_disabled_under_pytest_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without the opt-in, a test run never sweeps the temp root (no real $TMPDIR deletions)."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(_core, "_OUTPUT_DIR", None)
    monkeypatch.delenv("NERVE_OUTPUT_SWEEP", raising=False)
    stale = _old_output_dir(tmp_path, "nerve-output-stale")
    _core._output_dir()
    assert stale.exists()


def test_release_output_lock_closes_handle_and_tolerates_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The atexit hook closes the owner-lock handle; with no lock it is a no-op."""
    handle = retention.acquire_dir_lock(str(tmp_path))
    assert handle is not None
    monkeypatch.setattr(_core, "_OUTPUT_LOCK", handle)
    _core._release_output_lock()
    assert handle.closed
    assert retention.dir_is_live(str(tmp_path)) is False
    monkeypatch.setattr(_core, "_OUTPUT_LOCK", None)
    _core._release_output_lock()
