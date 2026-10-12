"""Branch coverage for pre-existing functions the post-linc-ops#1138 CRAP gate flags.

linc-ops#1138 made ``linc-crap-check --compare`` fail on any function absent
from ``.crap-baseline.json`` whose CRAP score is >= 30. Two untouched, long-lived
functions sat at exactly 30.0 (complexity 5, 0% branch coverage):

* ``linc_nerve.daemon.wda_manager._default_sim_states``
* ``linc_nerve.docs_gen.main``

This module is self-contained (no conftest fixtures) so it can be shared
verbatim across PRs. ``subprocess.run`` and the docs_gen ``check``/``write``
helpers are faked — nothing shells out and nothing writes into the repo.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from linc_nerve import docs_gen
from linc_nerve.daemon import wda_manager

# --------------------------------------------------------------------------- #
# wda_manager._default_sim_states
# --------------------------------------------------------------------------- #


def _fake_run(stdout: str):
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs.get("check") is True
        assert "shell" not in kwargs
        return SimpleNamespace(stdout=stdout, returncode=0)

    return run, calls


def test_sim_states_parses_every_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
                {"udid": "AAA", "state": "Booted"},
                {"udid": "BBB"},  # state missing -> Unknown
                {"name": "no-udid", "state": "Booted"},  # skipped
                {"udid": "", "state": "Booted"},  # falsy udid skipped
            ],
            "com.apple.CoreSimulator.SimRuntime.watchOS-11-0": [
                {"udid": "CCC", "state": "Shutdown"},
            ],
            "com.apple.CoreSimulator.SimRuntime.empty": [],
        }
    }
    run, calls = _fake_run(json.dumps(payload))
    monkeypatch.setattr(wda_manager.subprocess, "run", run)

    states = wda_manager._default_sim_states()

    assert states == {"AAA": "Booted", "BBB": "Unknown", "CCC": "Shutdown"}
    assert calls == [["xcrun", "simctl", "list", "devices", "--json"]]


def test_sim_states_without_devices_key_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _ = _fake_run(json.dumps({}))
    monkeypatch.setattr(wda_manager.subprocess, "run", run)

    assert wda_manager._default_sim_states() == {}


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("xcrun"),
        subprocess.TimeoutExpired(cmd="xcrun", timeout=15),
        subprocess.CalledProcessError(returncode=1, cmd="xcrun"),
    ],
)
def test_sim_states_probe_failure_returns_empty(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(wda_manager.subprocess, "run", boom)

    assert wda_manager._default_sim_states() == {}


def test_sim_states_bad_json_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = _fake_run("not json {")
    monkeypatch.setattr(wda_manager.subprocess, "run", run)

    assert wda_manager._default_sim_states() == {}


def test_sim_states_returns_empty_for_runtime_with_no_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-18-0": []}}
    run, _ = _fake_run(json.dumps(payload))
    monkeypatch.setattr(wda_manager.subprocess, "run", run)

    assert wda_manager._default_sim_states() == {}


# --------------------------------------------------------------------------- #
# docs_gen.main
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_docs(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace docs_gen.check/write so main() never touches the real repo."""
    state: dict = {"stale": [], "changed": [], "check_roots": [], "write_roots": []}

    def fake_check(root):
        state["check_roots"].append(root)
        return list(state["stale"])

    def fake_write(root):
        state["write_roots"].append(root)
        return list(state["changed"])

    monkeypatch.setattr(docs_gen, "check", fake_check)
    monkeypatch.setattr(docs_gen, "write", fake_write)
    return state


def test_main_check_stale_returns_1(fake_docs: dict, capsys) -> None:
    fake_docs["stale"] = ["CLAUDE.md", "README.md"]

    assert docs_gen.main(["--check"]) == 1

    out = capsys.readouterr().out
    assert "STALE generated docs: CLAUDE.md, README.md" in out
    assert "uv run python -m linc_nerve.docs_gen" in out
    assert fake_docs["write_roots"] == []


def test_main_check_clean_returns_0(fake_docs: dict, capsys) -> None:
    assert docs_gen.main(["--check"]) == 0

    assert "up to date" in capsys.readouterr().out
    assert len(fake_docs["check_roots"]) == 1
    assert fake_docs["write_roots"] == []


def test_main_write_reports_changed(fake_docs: dict, capsys) -> None:
    fake_docs["changed"] = ["CLAUDE.md"]

    assert docs_gen.main([]) == 0

    assert "Regenerated: CLAUDE.md" in capsys.readouterr().out
    assert fake_docs["check_roots"] == []


def test_main_write_already_up_to_date(fake_docs: dict, capsys) -> None:
    assert docs_gen.main([]) == 0

    assert "Already up to date." in capsys.readouterr().out
    assert len(fake_docs["write_roots"]) == 1


def test_main_defaults_to_sys_argv(
    fake_docs: dict, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(sys, "argv", ["docs_gen", "--check"])

    assert docs_gen.main() == 0

    assert len(fake_docs["check_roots"]) == 1
    root = fake_docs["check_roots"][0]
    # Two levels above the package dir: the repo root in a source checkout, the
    # site-packages parent in an installed wheel (the public selfcheck runs there).
    assert root == Path(docs_gen.__file__).resolve().parents[2]
