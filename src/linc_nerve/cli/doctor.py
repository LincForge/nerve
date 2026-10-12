"""`nerve doctor` — environment + daemon diagnostics with fix-it hints (issue #80).

One-command bootstrap entry point for a new machine (and the O3 "Agent-Native
QA Bootstrap" consulting offer). Architecture:

- **Probes** (impure, thin): `gather_probes()` collects raw facts about the
  host — binary paths, env vars, one `adb devices` run, one GET /health —
  into a flat, primitive-typed `ProbeReport`. Every probe is individually
  guarded; a probe failure becomes a check failure, never a crash.
- **Checks** (pure): each `check_*` function maps probe primitives to a
  `CheckResult` with a one-line fix-it hint on failure. Unit-testable with
  zero hardware, zero network, zero filesystem.
- **Fix planning** (pure): `plan_fixes()` maps failed checks to safe,
  idempotent `FixAction`s (create $LINC_HOME dirs, start the adb server).
  `--fix` applies them; `--fix --dry-run` prints the plan only.

Exit contract: 0 all-green (warns allowed), 1 if any check fails.
`--json` emits the full report for agent consumption.
"""

from __future__ import annotations

import dataclasses
import json as _json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import click

from linc_nerve.core.http_port import DEFAULT_HTTP_PORT, resolve_http_port
from linc_nerve.core.observability import linc_home

DAEMON_PORT = DEFAULT_HTTP_PORT
DAEMON_LABEL = "com.linc.nerve-daemon"


def required_linc_dirs() -> tuple[str, ...]:
    """$LINC_HOME paths nerve expects to exist (state root + recordings).

    Resolved at call time from ``core.observability.linc_home()`` so the
    check and the ``--fix`` plan follow ``LINC_HOME`` instead of ``~/.linc``.
    """
    home = linc_home()
    return (str(home), str(home / "nerve" / "recordings"))


Status = Literal["ok", "fail", "warn", "skip"]

_STATUS_ICON = {"ok": "✓", "fail": "✗", "warn": "!", "skip": "-"}


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one diagnostic check.

    ``hint`` is a one-line fix-it instruction, populated whenever
    ``status`` is ``fail`` (and optionally on ``warn``).
    """

    name: str
    status: Status
    detail: str
    hint: str = ""


@dataclass(frozen=True)
class FixAction:
    """A safe, idempotent remediation planned from a failed check."""

    kind: str
    description: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProbeReport:
    """Raw facts gathered from the host — primitives only, so every check
    stays a pure function testable without devices."""

    adb_path: str
    adb_is_executable: bool
    adb_devices_output: str | None  # None → adb server unresponsive / adb missing
    scrcpy_server_exists: bool
    scrcpy_server_path: str
    wda_project_dir: str
    wda_checkout_exists: bool
    wda_team_id: str
    is_macos: bool
    xcode_clt_path: str | None
    daemon_health: dict[str, Any] | None  # None → :port unreachable
    installed_version: str
    daemon_port: int = DAEMON_PORT
    linc_home_exists: bool = True
    missing_linc_dirs: tuple[str, ...] = field(default_factory=tuple)
    linc_home: str = ""  # resolved $LINC_HOME; "" → resolve at check time


# ---------------------------------------------------------------------------
# Checks — pure functions over probe primitives
# ---------------------------------------------------------------------------


def check_adb(
    adb_path: str, is_executable: bool, devices_output: str | None
) -> CheckResult:
    """adb binary present + server responsive."""
    if not is_executable:
        return CheckResult(
            name="adb",
            status="fail",
            detail=f"adb not found (resolved: {adb_path})",
            hint=(
                "Install Android platform-tools and/or set ANDROID_SDK_ROOT "
                "(brew install --cask android-platform-tools)"
            ),
        )
    if devices_output is None:
        return CheckResult(
            name="adb",
            status="fail",
            detail=f"adb present at {adb_path} but server unresponsive",
            hint="Run: adb start-server (or `nerve doctor --fix`)",
        )
    return CheckResult(name="adb", status="ok", detail=f"adb responsive ({adb_path})")


def check_device_auth(devices_output: str | None) -> CheckResult:
    """List devices stuck in `unauthorized` — they need the on-device RSA prompt."""
    if devices_output is None:
        return CheckResult(
            name="device-auth",
            status="skip",
            detail="skipped — adb server unresponsive",
        )
    unauthorized = [
        line.split("\t", 1)[0]
        for line in devices_output.splitlines()
        if "\tunauthorized" in line
    ]
    if unauthorized:
        return CheckResult(
            name="device-auth",
            status="fail",
            detail=f"unauthorized device(s): {', '.join(unauthorized)}",
            hint="Unlock the device and accept the 'Allow USB debugging' RSA prompt",
        )
    return CheckResult(name="device-auth", status="ok", detail="no unauthorized devices")


def check_scrcpy_server(exists: bool, expected_path: str) -> CheckResult:
    """Bundled scrcpy-server binary present (fast screenshot/stream path)."""
    if not exists:
        return CheckResult(
            name="scrcpy-server",
            status="fail",
            detail=f"scrcpy-server binary missing: {expected_path}",
            hint="Reinstall linc-nerve (uv sync) — the binary ships in src/linc_nerve/vendor/",
        )
    return CheckResult(name="scrcpy-server", status="ok", detail=expected_path)


def check_wda_project(project_dir: str, checkout_exists: bool) -> CheckResult:
    """WDA_PROJECT_DIR set and pointing at a real WebDriverAgent checkout.

    Unset is a warn (Android-only users never need WDA); set-but-missing is a
    fail — the user asked for iOS and pointed at nothing.
    """
    if not project_dir:
        return CheckResult(
            name="wda-project",
            status="warn",
            detail=(
                "WDA_PROJECT_DIR not set — iOS tap/swipe/elements unavailable "
                "(Android works without it)"
            ),
            hint=(
                "git clone https://github.com/appium/WebDriverAgent.git ~/.linc/wda/src "
                "&& export WDA_PROJECT_DIR=~/.linc/wda/src"
            ),
        )
    if not checkout_exists:
        return CheckResult(
            name="wda-project",
            status="fail",
            detail=f"WDA_PROJECT_DIR={project_dir} but no checkout there",
            hint=f"git clone https://github.com/appium/WebDriverAgent.git {project_dir}",
        )
    return CheckResult(name="wda-project", status="ok", detail=project_dir)


def check_wda_team_id(team_id: str) -> CheckResult:
    """WDA_TEAM_ID — only needed for physical iOS devices, so a warn, not a fail."""
    if not team_id:
        return CheckResult(
            name="wda-team-id",
            status="warn",
            detail="WDA_TEAM_ID not set — physical-iOS WDA signing will fail",
            hint="export WDA_TEAM_ID=<Apple Developer Team ID> (simulators don't need it)",
        )
    return CheckResult(name="wda-team-id", status="ok", detail="WDA_TEAM_ID set")


def check_xcode_clt(is_macos: bool, clt_path: str | None) -> CheckResult:
    """Xcode Command Line Tools present (macOS only)."""
    if not is_macos:
        return CheckResult(
            name="xcode-clt", status="skip", detail="skipped — not macOS"
        )
    if not clt_path:
        return CheckResult(
            name="xcode-clt",
            status="fail",
            detail="Xcode Command Line Tools not found",
            hint="Run: xcode-select --install",
        )
    return CheckResult(name="xcode-clt", status="ok", detail=clt_path)


def check_daemon(
    health: dict[str, Any] | None, installed_version: str, port: int
) -> CheckResult:
    """Daemon reachable on :port AND serving the installed package version.

    A version mismatch means a long-lived daemon is serving stale pre-merge
    code (issue #79) — the fix is a launchctl kickstart, not a reinstall.
    No daemon at all is only a warn: the CLI falls back to running tools
    in-process, so the daemon is optional.
    """
    if health is None:
        return CheckResult(
            name="daemon",
            status="warn",
            detail=(
                f"no nerve daemon on 127.0.0.1:{port} — CLI calls run in-process "
                "(the daemon is optional)"
            ),
            hint=(
                f"Start it: uv run nerve daemon --port {port} "
                f"(or launchctl kickstart -k gui/$UID/{DAEMON_LABEL})"
            ),
        )
    served = str(health.get("package_version", "unknown"))
    if served == "unknown" or installed_version == "unknown":
        return CheckResult(
            name="daemon",
            status="warn",
            detail=(
                f"daemon reachable on :{port} but version unverifiable "
                f"(served={served}, installed={installed_version})"
            ),
            hint="Install the package (uv sync) so version metadata is available",
        )
    if served != installed_version:
        return CheckResult(
            name="daemon",
            status="fail",
            detail=(
                f"stale daemon: serving {served}, installed {installed_version}"
            ),
            hint=f"launchctl kickstart -k gui/$UID/{DAEMON_LABEL}",
        )
    return CheckResult(
        name="daemon", status="ok", detail=f"reachable on :{port}, version {served}"
    )


def check_linc_layout(
    home_exists: bool, missing_dirs: tuple[str, ...], home: str = ""
) -> CheckResult:
    """$LINC_HOME layout sane (state + recordings directories).

    Missing directories are a warn: nerve creates them on first use (the usage
    log and recordings both ``mkdir(parents=True)``), so a fresh host is fine.
    ``home`` is the resolved state root shown in detail/hint; empty means
    resolve ``linc_home()`` now.
    """
    home = home or str(linc_home())
    if not home_exists or missing_dirs:
        missing = missing_dirs or (home,)
        return CheckResult(
            name="linc-layout",
            status="warn",
            detail=f"missing directories: {', '.join(missing)} — nerve creates them on first use",
            hint=f"mkdir -p {' '.join(missing)} (or `nerve doctor --fix`)",
        )
    return CheckResult(name="linc-layout", status="ok", detail=f"{home} layout sane")


def run_checks(probes: ProbeReport) -> list[CheckResult]:
    """Run every diagnostic check over the gathered probes. Pure."""
    return [
        check_adb(probes.adb_path, probes.adb_is_executable, probes.adb_devices_output),
        check_device_auth(probes.adb_devices_output),
        check_scrcpy_server(probes.scrcpy_server_exists, probes.scrcpy_server_path),
        check_wda_project(probes.wda_project_dir, probes.wda_checkout_exists),
        check_wda_team_id(probes.wda_team_id),
        check_xcode_clt(probes.is_macos, probes.xcode_clt_path),
        check_daemon(probes.daemon_health, probes.installed_version, probes.daemon_port),
        check_linc_layout(probes.linc_home_exists, probes.missing_linc_dirs, probes.linc_home),
    ]


def overall_ok(results: list[CheckResult]) -> bool:
    """Exit-code contract: warns and skips are fine; any fail flips to 1."""
    return not any(r.status == "fail" for r in results)


# ---------------------------------------------------------------------------
# Bootstrap fix planning — pure plan, injectable executor
# ---------------------------------------------------------------------------


def plan_fixes(
    results: list[CheckResult], linc_dirs: tuple[str, ...] | None = None
) -> list[FixAction]:
    """Map failed (or, for the $LINC_HOME layout, warned) checks to safe,
    idempotent remediations.

    Only auto-fixable problems get an action: directory creation and adb
    server start. Everything else stays a hint for the human/agent.
    ``linc_dirs`` defaults to ``required_linc_dirs()`` resolved now.
    """
    fixes: list[FixAction] = []
    by_name = {r.name: r for r in results}

    layout = by_name.get("linc-layout")
    if layout is not None and layout.status != "ok":
        dirs = linc_dirs or required_linc_dirs()
        fixes.append(
            FixAction(
                kind="mkdir",
                description=f"create directories: {', '.join(dirs)}",
                args=dirs,
            )
        )

    adb = by_name.get("adb")
    if adb is not None and adb.status == "fail" and "unresponsive" in adb.detail:
        fixes.append(
            FixAction(
                kind="adb-start-server",
                description="start the adb server (adb start-server)",
            )
        )
    return fixes


def _execute_fix(fix: FixAction) -> None:
    """Apply one FixAction to the real host. Impure by design."""
    if fix.kind == "mkdir":
        for d in fix.args:
            Path(d).expanduser().mkdir(parents=True, exist_ok=True)
    elif fix.kind == "adb-start-server":
        from linc_nerve.core.adb_path import ADB_PATH

        subprocess.run([ADB_PATH, "start-server"], check=False, timeout=15)


def apply_fixes(
    fixes: list[FixAction],
    *,
    dry_run: bool,
    executor: Any = _execute_fix,
) -> list[FixAction]:
    """Apply planned fixes unless ``dry_run``. Returns the fixes applied."""
    if dry_run:
        return []
    for fix in fixes:
        executor(fix)
    return fixes


# ---------------------------------------------------------------------------
# Probes — the only impure data gathering
# ---------------------------------------------------------------------------


def _probe_adb_devices(adb_path: str) -> str | None:
    """One `adb devices` run — proves the server is (or can get) responsive."""
    try:
        proc = subprocess.run(
            [adb_path, "devices"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _probe_xcode_clt() -> str | None:
    try:
        proc = subprocess.run(
            ["xcode-select", "-p"], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    path = proc.stdout.strip()
    return path if proc.returncode == 0 and path else None


def _probe_daemon_health(port: int) -> dict[str, Any] | None:
    """GET /health with a tight timeout — None means unreachable/unhealthy.

    Uses urllib (not httpx) so the probe emits zero log lines — `--json`
    output must stay parseable by agents.
    """
    import urllib.request

    try:
        # B310: scheme and host are hardcoded (http://127.0.0.1); only the
        # int-typed port varies — no user-controlled URL can reach this.
        with urllib.request.urlopen(  # nosec B310
            f"http://127.0.0.1:{port}/health", timeout=2.0
        ) as resp:
            if resp.status != 200:
                return None
            payload = _json.loads(resp.read().decode())
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _probe_installed_version() -> str:
    try:
        import importlib.metadata

        return importlib.metadata.version("linc-nerve")
    except Exception:
        return "unknown"


def gather_probes(port: int | None = None) -> ProbeReport:
    """Collect raw host facts for the pure checks. Each probe is guarded.

    ``port`` resolves like the server binds: explicit > NERVE_HTTP_PORT > 8111.
    """
    port = resolve_http_port(port)
    from linc_nerve.core import scrcpy
    from linc_nerve.core.adb_path import ADB_PATH

    adb_executable = shutil.which(ADB_PATH) is not None or (
        os.path.isabs(ADB_PATH) and os.access(ADB_PATH, os.X_OK)
    )
    devices_output = _probe_adb_devices(ADB_PATH) if adb_executable else None

    scrcpy_path = scrcpy._SERVER_DIR / scrcpy._SERVER_FILENAME

    wda_project_dir = os.environ.get("WDA_PROJECT_DIR", "")
    wda_checkout_exists = bool(
        wda_project_dir and Path(wda_project_dir).expanduser().is_dir()
    )

    is_macos = platform.system() == "Darwin"

    home = linc_home()
    missing_dirs = tuple(d for d in required_linc_dirs() if not Path(d).is_dir())

    return ProbeReport(
        adb_path=ADB_PATH,
        adb_is_executable=adb_executable,
        adb_devices_output=devices_output,
        scrcpy_server_exists=scrcpy.server_available(),
        scrcpy_server_path=str(scrcpy_path),
        wda_project_dir=wda_project_dir,
        wda_checkout_exists=wda_checkout_exists,
        wda_team_id=os.environ.get("WDA_TEAM_ID", ""),
        is_macos=is_macos,
        xcode_clt_path=_probe_xcode_clt() if is_macos else None,
        daemon_health=_probe_daemon_health(port),
        installed_version=_probe_installed_version(),
        daemon_port=port,
        linc_home_exists=home.is_dir(),
        missing_linc_dirs=missing_dirs,
        linc_home=str(home),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _render_text(results: list[CheckResult], fixes: list[FixAction], dry_run: bool) -> str:
    lines = []
    for r in results:
        lines.append(f"  {_STATUS_ICON[r.status]} {r.name}: {r.detail}")
        if r.hint and r.status in ("fail", "warn"):
            lines.append(f"      hint: {r.hint}")
    fails = sum(1 for r in results if r.status == "fail")
    warns = sum(1 for r in results if r.status == "warn")
    lines.append("")
    lines.append(
        f"{len(results)} checks: {fails} failed, {warns} warned"
        if fails or warns
        else f"{len(results)} checks: all green"
    )
    if fixes:
        header = "planned fixes (dry-run — nothing executed):" if dry_run else "applied fixes:"
        lines.append(header)
        lines.extend(f"  * {f.description}" for f in fixes)
    return "\n".join(lines)


@click.command()
@click.option("--json", "json_output", is_flag=True, help="Machine-readable report")
@click.option("--fix", "do_fix", is_flag=True, help="Apply safe fixes (mkdir, adb start-server)")
@click.option("--dry-run", is_flag=True, help="With --fix: print the plan, execute nothing")
@click.option(
    "--port",
    type=int,
    default=None,
    help=f"Daemon port to probe [default: $NERVE_HTTP_PORT or {DAEMON_PORT}]",
)
def doctor(json_output: bool, do_fix: bool, dry_run: bool, port: int | None) -> None:
    """Diagnose the nerve environment — one-command bootstrap entry point.

    Exits 0 when all checks pass (warnings allowed), 1 when any check
    fails; every failure carries a one-line fix-it hint.
    """
    probes = gather_probes(port=port)
    results = run_checks(probes)
    fixes = plan_fixes(results) if (do_fix or dry_run) else []
    if do_fix:
        apply_fixes(fixes, dry_run=dry_run, executor=_execute_fix)

    if json_output:
        payload = {
            "ok": overall_ok(results),
            "checks": [dataclasses.asdict(r) for r in results],
            "fixes": [dataclasses.asdict(f) for f in fixes],
            "dry_run": dry_run,
        }
        click.echo(_json.dumps(payload, indent=2))
    else:
        click.echo(_render_text(results, fixes, dry_run))

    sys.exit(0 if overall_ok(results) else 1)
