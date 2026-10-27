"""`nerve doctor` honours LINC_HOME and NERVE_HTTP_PORT (bead linc-nerve-iri).

Stranger-run 2026-10-02 (LINC docs/reviews/2026-10-02-linc-nerve-0.1.0-
stranger-run.md, items 2 + 9): with ``LINC_HOME`` pointing at an empty dir,
doctor still reported "~/.linc layout sane"; with ``NERVE_HTTP_PORT=18111``
it still probed :8111. Both are resolved at call time now.

The impure host probes (adb, xcode-select, /health) are monkeypatched so
these tests touch no devices and no network — only ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from linc_nerve.cli import doctor as doctor_mod
from linc_nerve.cli.doctor import (
    _execute_fix,
    check_daemon,
    check_linc_layout,
    doctor,
    gather_probes,
    plan_fixes,
    required_linc_dirs,
    run_checks,
)
from linc_nerve.core.http_port import DEFAULT_HTTP_PORT, resolve_http_port
from linc_nerve.core.observability import linc_home


@pytest.fixture
def probed_ports(monkeypatch) -> list[int]:
    """Stub every impure probe; record the port the /health probe targets."""
    ports: list[int] = []

    def _fake_health(port: int):
        ports.append(port)
        return None

    monkeypatch.setattr(doctor_mod, "_probe_daemon_health", _fake_health)
    monkeypatch.setattr(doctor_mod, "_probe_adb_devices", lambda _adb: (None, None))
    monkeypatch.setattr(doctor_mod, "_adb_server_probe", lambda _addr: doctor_mod.AdbProbe.UNKNOWN)
    monkeypatch.setattr(doctor_mod, "_probe_xcode_clt", lambda: None)
    return ports


@pytest.fixture
def fresh_home(monkeypatch, tmp_path) -> Path:
    """LINC_HOME pointed at a directory that does not exist yet."""
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("LINC_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# linc_home() — the single resolver doctor + observability share
# ---------------------------------------------------------------------------


class TestLincHome:
    def test_honours_env(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("LINC_HOME", str(tmp_path))
        assert linc_home() == tmp_path

    def test_defaults_to_dot_linc(self, monkeypatch) -> None:
        monkeypatch.delenv("LINC_HOME", raising=False)
        assert linc_home() == Path.home() / ".linc"

    def test_expands_user(self, monkeypatch) -> None:
        monkeypatch.setenv("LINC_HOME", "~/custom-linc")
        assert linc_home() == Path.home() / "custom-linc"


# ---------------------------------------------------------------------------
# LINC_HOME — linc-layout check + --fix plan
# ---------------------------------------------------------------------------


class TestLincLayoutHonoursLincHome:
    def test_required_dirs_resolved_at_call_time(self, fresh_home) -> None:
        assert required_linc_dirs() == (
            str(fresh_home),
            str(fresh_home / "nerve" / "recordings"),
        )

    def test_layout_check_references_linc_home(self, fresh_home, probed_ports) -> None:
        probes = gather_probes()
        assert probes.linc_home == str(fresh_home)
        assert probes.linc_home_exists is False
        layout = next(r for r in run_checks(probes) if r.name == "linc-layout")
        assert layout.status == "warn"
        recordings = str(fresh_home / "nerve" / "recordings")
        assert str(fresh_home) in layout.detail
        assert recordings in layout.detail
        assert recordings in layout.hint
        assert "~/.linc" not in layout.detail + layout.hint

    def test_fix_plan_references_linc_home(self, fresh_home, probed_ports) -> None:
        fixes = plan_fixes(run_checks(gather_probes()))
        mkdir = next(f for f in fixes if f.kind == "mkdir")
        expected = (str(fresh_home), str(fresh_home / "nerve" / "recordings"))
        assert mkdir.args == expected
        assert "~/.linc" not in mkdir.description

    def test_fix_creates_dirs_under_linc_home(self, fresh_home, probed_ports) -> None:
        for fix in plan_fixes(run_checks(gather_probes())):
            if fix.kind == "mkdir":
                _execute_fix(fix)
        assert (fresh_home / "nerve" / "recordings").is_dir()
        layout = next(r for r in run_checks(gather_probes()) if r.name == "linc-layout")
        assert layout.status == "ok"
        assert str(fresh_home) in layout.detail

    def test_existing_dot_linc_does_not_mask_empty_linc_home(
        self, fresh_home, probed_ports
    ) -> None:
        """The stranger-run symptom: a real ~/.linc must not satisfy the check."""
        layout = next(r for r in run_checks(gather_probes()) if r.name == "linc-layout")
        assert layout.status == "warn"

    def test_cli_dry_run_plan_prints_linc_home(self, fresh_home, probed_ports) -> None:
        result = CliRunner().invoke(doctor, ["--json", "--fix", "--dry-run"])
        payload = json.loads(result.output)
        mkdir = next(f for f in payload["fixes"] if f["kind"] == "mkdir")
        assert mkdir["args"] == [
            str(fresh_home),
            str(fresh_home / "nerve" / "recordings"),
        ]
        assert not fresh_home.exists()

    def test_check_default_home_label_is_resolved(self, fresh_home) -> None:
        r = check_linc_layout(False, ())
        assert str(fresh_home) in r.detail
        assert str(fresh_home) in r.hint

    def test_plan_fixes_explicit_dirs_win(self, fresh_home) -> None:
        results = [check_linc_layout(False, ("/x",), home="/x")]
        mkdir = plan_fixes(results, linc_dirs=("/x", "/x/nerve/recordings"))[0]
        assert mkdir.args == ("/x", "/x/nerve/recordings")


# ---------------------------------------------------------------------------
# NERVE_HTTP_PORT — daemon probe
# ---------------------------------------------------------------------------


class TestDaemonProbeHonoursHttpPort:
    def test_env_port_is_probed(self, monkeypatch, probed_ports) -> None:
        monkeypatch.setenv("NERVE_HTTP_PORT", "18111")
        probes = gather_probes()
        assert probed_ports == [18111]
        assert probes.daemon_port == 18111

    def test_explicit_port_beats_env(self, monkeypatch, probed_ports) -> None:
        monkeypatch.setenv("NERVE_HTTP_PORT", "18111")
        gather_probes(port=9999)
        assert probed_ports == [9999]

    def test_default_port_without_env(self, monkeypatch, probed_ports) -> None:
        monkeypatch.delenv("NERVE_HTTP_PORT", raising=False)
        gather_probes()
        assert probed_ports == [DEFAULT_HTTP_PORT]

    def test_cli_without_port_flag_uses_env(self, monkeypatch, probed_ports) -> None:
        monkeypatch.setenv("NERVE_HTTP_PORT", "18111")
        result = CliRunner().invoke(doctor, ["--json"])
        assert probed_ports == [18111]
        daemon = next(c for c in json.loads(result.output)["checks"] if c["name"] == "daemon")
        assert "18111" in daemon["detail"]
        assert "--port 18111" in daemon["hint"]

    def test_cli_port_flag_beats_env(self, monkeypatch, probed_ports) -> None:
        monkeypatch.setenv("NERVE_HTTP_PORT", "18111")
        CliRunner().invoke(doctor, ["--json", "--port", "9999"])
        assert probed_ports == [9999]

    def test_unreachable_hint_names_resolved_port(self) -> None:
        r = check_daemon(None, "0.1.0", 18111)
        assert "18111" in r.detail
        assert "18111" in r.hint


# ---------------------------------------------------------------------------
# Shared resolver — same order the HTTP server uses
# ---------------------------------------------------------------------------


class TestResolveHttpPort:
    def test_explicit_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("NERVE_HTTP_PORT", "18111")
        assert resolve_http_port(9999) == 9999

    def test_env_next(self, monkeypatch) -> None:
        monkeypatch.setenv("NERVE_HTTP_PORT", "18111")
        assert resolve_http_port(None) == 18111

    def test_default_last(self, monkeypatch) -> None:
        monkeypatch.delenv("NERVE_HTTP_PORT", raising=False)
        assert resolve_http_port(None) == DEFAULT_HTTP_PORT == 8111

    def test_invalid_env_falls_back_and_reports(self, monkeypatch) -> None:
        monkeypatch.setenv("NERVE_HTTP_PORT", "not-a-port")
        seen: list[str] = []
        assert resolve_http_port(None, on_invalid=seen.append) == DEFAULT_HTTP_PORT
        assert seen == ["not-a-port"]

    def test_invalid_env_silent_without_callback(self, monkeypatch) -> None:
        monkeypatch.setenv("NERVE_HTTP_PORT", "nope")
        assert resolve_http_port(None) == DEFAULT_HTTP_PORT

    def test_server_uses_shared_resolver(self, monkeypatch) -> None:
        from linc_nerve.mcp import sse_server

        monkeypatch.setenv("NERVE_HTTP_PORT", "18111")
        assert sse_server._resolve_http_port(None) == 18111
        assert sse_server._resolve_http_port(7) == 7
        assert sse_server.SSE_PORT == DEFAULT_HTTP_PORT

    def test_server_logs_invalid_env(self, monkeypatch) -> None:
        from linc_nerve.mcp import sse_server

        warned: list[dict] = []
        monkeypatch.setattr(
            sse_server.log, "warning", lambda event, **kw: warned.append({"event": event, **kw})
        )
        monkeypatch.setenv("NERVE_HTTP_PORT", "bogus")
        assert sse_server._resolve_http_port(None) == DEFAULT_HTTP_PORT
        assert warned == [{"event": "nerve_http_port_invalid", "value": "bogus"}]
