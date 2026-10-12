"""Packaging tests ensuring built distributions contain all required assets."""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


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
