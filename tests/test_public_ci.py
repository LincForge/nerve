"""The public repo's CI must run README quickstart verbatim and use trusted publishing."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
GH = REPO / "public" / ".github" if (REPO / "public" / ".github").is_dir() else REPO / ".github"
WF = GH / "workflows"


def _wf(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text())


def _quickstart_commands() -> list[str]:
    readme = (REPO / "README.md").read_text()
    match = re.search(r"<!-- quickstart:start -->(.*?)<!-- quickstart:end -->", readme, re.S)
    assert match, "README must fence the quickstart"
    block = match.group(1)
    cmds = []
    for fence in re.findall(r"```(?:bash|sh)\n(.*?)```", block, re.S):
        cmds += [
            ln.strip() for ln in fence.splitlines() if ln.strip().startswith(("uvx ", "nerve "))
        ]
    return cmds


def test_public_ci_workflow_files_exist() -> None:
    assert (WF / "test.yml").is_file()
    assert (WF / "smoke.yml").is_file()
    assert (WF / "release.yml").is_file()
    assert (GH / "dependabot.yml").is_file()


def test_smoke_runs_every_quickstart_command_verbatim() -> None:
    smoke = (WF / "smoke.yml").read_text()
    cmds = _quickstart_commands()
    assert cmds, "no quickstart commands found"
    missing = [c for c in cmds if c not in smoke]
    assert missing == [], f"smoke.yml does not run: {missing}"


def test_release_uses_trusted_publishing_no_token() -> None:
    rel = _wf("release.yml")
    job = next(iter(rel["jobs"].values()))
    assert job["permissions"]["id-token"] == "write"
    text = (WF / "release.yml").read_text()
    assert "pypa/gh-action-pypi-publish" in text
    assert "password" not in text and "PYPI_TOKEN" not in text


def test_workflows_reference_no_private_repos_or_secrets() -> None:
    for f in WF.glob("*.yml"):
        text = f.read_text()
        assert "secrets." not in text, f
        assert "LincForge/linc-" not in text, f


def test_dependabot_version_updates_disabled() -> None:
    cfg = yaml.safe_load((GH / "dependabot.yml").read_text())
    assert all(u.get("open-pull-requests-limit") == 0 for u in cfg["updates"])
