"""Public test: tool count in README.md matches the live MCP surface and registry.

This test is part of the public package (not .publishignore) and verifies that:
1. README inline count == heading count == len(server.list_tools()) == len(TOOL_REGISTRY).
2. set(TOOL_REGISTRY) == live tool names.
3. docs_gen.render_block() renders the live count even when TOOL_REGISTRY has extra entries.
4. docs_gen.check() reports registry vs live mismatches.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from linc_nerve import docs_gen
from linc_nerve.mcp import _core
from linc_nerve.mcp import server as mcp_mod  # noqa: F401  (registers all tools)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_readme_tool_count_matches_live_and_registry() -> None:
    """README inline count == heading count == len(list_tools()) == len(TOOL_REGISTRY)."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    inline_pattern = re.compile(r"<!-- NERVE:TOOL-COUNT -->(\d+)<!-- /NERVE:TOOL-COUNT -->")
    inline_matches = inline_pattern.findall(readme)
    assert inline_matches, "README.md missing inline tool count marker"
    inline_count = int(inline_matches[0])

    heading_pattern = re.compile(r"^## MCP Tools \((\d+)\)", re.MULTILINE)
    heading_matches = heading_pattern.findall(readme)
    assert heading_matches, "README.md missing '## MCP Tools (N)' heading"
    heading_count = int(heading_matches[0])

    live_tools = asyncio.run(_core.server.list_tools())
    live_names = sorted(tool.name for tool in live_tools)
    live_count = len(live_tools)
    reg_names = sorted(_core.TOOL_REGISTRY.keys())
    reg_count = len(_core.TOOL_REGISTRY)

    assert set(reg_names) == set(live_names), (
        f"Registry and live surface differ: "
        f"registry only {set(reg_names) - set(live_names)}, "
        f"live only {set(live_names) - set(reg_names)}"
    )
    assert inline_count == heading_count == live_count == reg_count, (
        f"Count mismatch: inline={inline_count}, heading={heading_count}, "
        f"live={live_count}, registry={reg_count}"
    )


def test_fake_registry_entry_render_and_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With a fake extra entry in _core.TOOL_REGISTRY, render_block() renders live
    count and check() reports the registry/live mismatch."""
    fake_name = "nerve_fake_unregistered_mcp_tool"
    fake_meta = {
        "kind": "device",
        "module": "linc_nerve.mcp.fake",
        "description": "Fake tool for testing",
    }
    monkeypatch.setitem(_core.TOOL_REGISTRY, fake_name, fake_meta)

    # 1. render_block() must render the live count, NOT len(TOOL_REGISTRY)
    live_count = len(docs_gen.live_tool_names())
    block = docs_gen.render_block()
    assert f"## MCP Tools ({live_count})" in block
    assert f"## MCP Tools ({live_count + 1})" not in block

    # 2. check() must report the mismatch
    stale = docs_gen.check(REPO_ROOT)
    assert any("mismatch" in s or fake_name in s for s in stale), (
        f"Expected check() to report registry/live mismatch, got: {stale}"
    )

    # 3. main(["--check"]) exits 1 and prints both sets' differences
    exit_code = docs_gen.main(["--check"])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert fake_name in out
    assert "In registry but not live" in out
    assert "In live but not registry" in out


def test_fake_live_tool_missing_from_registry_reports_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the live surface exposes a tool not in _core.TOOL_REGISTRY,
    docs_gen.check() reports the mismatch and main(["--check"]) exits 1."""
    fake_name = "nerve_fake_unregistered_live_tool"

    orig_live = docs_gen.live_tool_names
    monkeypatch.setattr(docs_gen, "live_tool_names", lambda: sorted([*orig_live(), fake_name]))

    stale = docs_gen.check(REPO_ROOT)
    assert any("mismatch" in s or fake_name in s for s in stale)

    exit_code = docs_gen.main(["--check"])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert fake_name in out
    assert "In registry but not live" in out
    assert "In live but not registry" in out


def test_check_tolerates_missing_linc_md(tmp_path: pathlib.Path) -> None:
    """The public export ships README.md but not LINC.md; check() must not
    raise FileNotFoundError on that tree."""
    (tmp_path / "README.md").write_text((REPO_ROOT / "README.md").read_text(encoding="utf-8"))
    assert not (tmp_path / "LINC.md").exists()
    assert docs_gen.check(tmp_path) == []
