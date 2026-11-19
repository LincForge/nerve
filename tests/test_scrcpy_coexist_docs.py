"""Tests for the Coexisting with scrcpy documentation note (linc-nerve-ntkv)."""

from __future__ import annotations

from pathlib import Path

import pytest

# Import ios before anything else: the autouse conftest fixture imports core.ios_discovery,
# which hits an ios <-> ios_discovery import cycle unless core.ios is already loaded. Without
# this line the file errors when run on its own (as tests/test_public_docs.py does).
from linc_nerve.core import ios as _ios_preload  # noqa: F401
from linc_nerve.core.scrcpy import _DEVICE_PATH, SCRCPY_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_readme_has_coexisting_with_scrcpy_note() -> None:
    """[test] README.md documents Coexisting with scrcpy."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "### Coexisting with scrcpy" in readme
    section = readme.split("### Coexisting with scrcpy", 1)[1].split("\n## ", 1)[0]
    assert f"scrcpy-server-v{SCRCPY_VERSION}" in section
    assert _DEVICE_PATH in section
    assert "localabstract:scrcpy_6e65" in section
    assert "/data/local/tmp/scrcpy-server.jar" in section


def test_linc_md_operational_notes_has_matching_scrcpy_coexistence_line() -> None:
    """[test] LINC.md Operational Notes carries a matching scrcpy coexistence note."""
    linc_md_path = REPO_ROOT / "LINC.md"
    if not linc_md_path.is_file():
        pytest.skip("LINC.md not present (public export)")
    linc_md = linc_md_path.read_text(encoding="utf-8")
    op_notes = linc_md.split("## Operational Notes", 1)[1].split("\n## ", 1)[0]
    assert _DEVICE_PATH in op_notes
    assert "localabstract:scrcpy_6e65" in op_notes
