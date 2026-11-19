"""Packaging tests ensuring built distributions contain all required assets."""

import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

import linc_nerve.core.ios  # noqa: F401

REPO = Path(__file__).resolve().parents[1]

_MIN_PATCHED_LOCK_VERSIONS: dict[str, str] = {
    "anyio": "4.14.2",
    "certifi": "2026.10.18",
    "cryptography": "50.0.0",
    "filelock": "4.2.1",
    "idna": "3.15",
    "mcp": "2.3.0",
    "oauthlib": "4.0.1",
    "opentelemetry-api": "1.45.1",
    "pillow": "13.0.0",
    "pyasn1": "0.6.5",
    "pygments": "2.20.0",
    "pyjwt": "2.15.0",
    "pytest": "9.0.3",
    "python-multipart": "0.0.31",
    "requests": "2.33.0",
    "starlette": "1.3.1",
    "urllib3": "2.8.0",
    "virtualenv": "21.14.9",
}


def test_uv_lock_patched_releases() -> None:
    """Every package with an open Dependabot alert in linc-nerve-adu9 is pinned >= patched."""
    lock_path = REPO / "uv.lock"
    if not lock_path.is_file():
        pytest.skip("uv.lock not present")

    with lock_path.open("rb") as f:
        lock_data = tomllib.load(f)

    versions_by_pkg: dict[str, list[str]] = {}
    for pkg in lock_data.get("package", []):
        name = pkg.get("name", "").lower()
        ver = pkg.get("version")
        if name and ver:
            versions_by_pkg.setdefault(name, []).append(ver)

    unpatched: list[str] = []
    for pkg_name, min_ver_str in sorted(_MIN_PATCHED_LOCK_VERSIONS.items()):
        locked_versions = versions_by_pkg.get(pkg_name, [])
        if not locked_versions:
            unpatched.append(f"{pkg_name}: missing from uv.lock (need >={min_ver_str})")
            continue
        min_ver = Version(min_ver_str)
        for ver_str in locked_versions:
            if Version(ver_str) < min_ver:
                unpatched.append(f"{pkg_name}=={ver_str} < {min_ver_str}")

    assert not unpatched, f"Vulnerable package pins in uv.lock: {unpatched}"


def _locked_versions(name: str) -> list[Version]:
    with (REPO / "uv.lock").open("rb") as f:
        lock_data = tomllib.load(f)
    return [
        Version(pkg["version"])
        for pkg in lock_data.get("package", [])
        if pkg.get("name", "").lower() == name and pkg.get("version")
    ]


def test_mcp_2x_declared_and_locked() -> None:
    """[test] mcp 2.x (mcp-2x molecule): pyproject declares `mcp>=2.3.0,<3`; the lock is in range.

    pydantic-settings left _MIN_PATCHED_LOCK_VERSIONS with this move: mcp 2.x no longer
    depends on it, so it is not in uv.lock at all.
    """
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    mcp_reqs = [
        Requirement(dep)
        for dep in pyproject["project"]["dependencies"]
        if Requirement(dep).name == "mcp"
    ]
    assert [str(r.specifier) for r in mcp_reqs] == ["<3,>=2.3.0"]
    assert "pydantic-settings" not in {p.lower() for p in _MIN_PATCHED_LOCK_VERSIONS}
    if not (REPO / "uv.lock").is_file():
        pytest.skip("uv.lock not present")
    locked = _locked_versions("mcp")
    assert locked, "mcp missing from uv.lock"
    assert all(Version("2.3.0") <= v < Version("3") for v in locked), locked


def test_public_workflows_declare_read_only_permissions() -> None:
    """public/.github/workflows/{test,smoke}.yml declare top-level read-only permissions."""
    gh_dir = (
        REPO / "public" / ".github" if (REPO / "public" / ".github").is_dir() else REPO / ".github"
    )
    workflows_dir = gh_dir / "workflows"
    for name in ("test.yml", "smoke.yml"):
        wf_path = workflows_dir / name
        assert wf_path.is_file(), f"Missing workflow file: {wf_path}"
        wf = yaml.safe_load(wf_path.read_text())
        assert wf.get("permissions") == {"contents": "read"}, (
            f"{wf_path} must declare top-level permissions == {{'contents': 'read'}}, "
            f"got {wf.get('permissions')!r}"
        )


def test_pyproject_dev_extra_lists_packaging() -> None:
    """pyproject.toml [project.optional-dependencies].dev explicitly lists packaging."""
    pyproject_path = REPO / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        pyproject_data = tomllib.load(f)

    dev_deps = pyproject_data.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    assert "packaging>=24" in dev_deps, (
        f"packaging>=24 missing from [project.optional-dependencies].dev: {dev_deps}"
    )


def test_pyproject_pillow_minimum_floor() -> None:
    """pyproject.toml [project].dependencies requires pillow >= 13.0.0."""
    pyproject_path = REPO / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        pyproject_data = tomllib.load(f)

    deps = [Requirement(d) for d in pyproject_data.get("project", {}).get("dependencies", [])]
    pillow_reqs = [r for r in deps if r.name.lower() == "pillow"]
    assert len(pillow_reqs) == 1
    req = pillow_reqs[0]
    lower_bounds = [
        Version(spec.version) for spec in req.specifier if spec.operator in (">=", "==")
    ]
    assert lower_bounds, f"No lower bound on pillow requirement: {req}"
    assert min(lower_bounds) >= Version("13.0.0"), f"Expected pillow minimum >= 13.0.0, got {req}"


def test_pyproject_pytest_asyncio_default_fixture_loop_scope() -> None:
    """[contract] [tool.pytest.ini_options] defines asyncio_default_fixture_loop_scope."""
    with (REPO / "pyproject.toml").open("rb") as f:
        pyproject_data = tomllib.load(f)

    ini_options = pyproject_data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert ini_options.get("asyncio_default_fixture_loop_scope") == "function"
    assert "error" in ini_options.get("filterwarnings", [])


def test_wheel_contains_package_data(tmp_path: Path) -> None:
    if not (REPO / "src").is_dir():
        pytest.skip("Source directory src/ not present (running against installed package)")

    has_build = False
    try:
        import build  # noqa: F401

        has_build = True
    except ImportError:
        has_build = False

    uv_path = shutil.which("uv")
    if has_build:
        cmd = [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(tmp_path),
            str(REPO),
        ]
    elif uv_path:
        cmd = [
            uv_path,
            "build",
            "--wheel",
            "--out-dir",
            str(tmp_path),
            "--directory",
            str(REPO),
        ]
    else:
        pytest.skip("Neither python -m build nor uv CLI is available to build wheels")

    try:
        proc = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        shutil.rmtree(REPO / "build", ignore_errors=True)
        shutil.rmtree(REPO / "src" / "linc_nerve.egg-info", ignore_errors=True)

    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, f"Expected 1 wheel in {tmp_path}, got {wheels}. Output: {proc.stdout}"

    with zipfile.ZipFile(wheels[0]) as zf:
        namelist = set(zf.namelist())

    assert "linc_nerve/mcp/routes.yaml" in namelist, (
        f"routes.yaml missing from wheel namelist: {sorted(namelist)}"
    )
    assert "linc_nerve/vendor/scrcpy-server-v3.3.4" in namelist, (
        f"scrcpy-server missing from wheel namelist: {sorted(namelist)}"
    )
