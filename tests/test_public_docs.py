"""Public-facing files a stranger relies on. Content checks only — prose is reviewed by the CEO."""

import json
import re
import shlex
import tomllib
from pathlib import Path

from click.testing import CliRunner

import linc_nerve
from linc_nerve.cli.main import main

REPO = Path(__file__).resolve().parents[1]
PUBLIC_REPO = "https://github.com/LincForge/nerve"
PUBLIC_BLOB = f"{PUBLIC_REPO}/blob/main/"
# Inline markdown links/images `](target)` and HTML href/src attributes.
_LINK_TARGET = re.compile(r"\]\(\s*<?([^)\s>]+)|(?:href|src)=[\"']([^\"']+)[\"']")


def _readme() -> str:
    return (REPO / "README.md").read_text()


def _pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text())


def test_licence_is_apache_everywhere() -> None:
    assert "Apache License" in (REPO / "LICENSE").read_text()
    assert "Version 2.0" in (REPO / "LICENSE").read_text()
    project = _pyproject()["project"]
    assert project["license"] == "Apache-2.0"
    assert not any("MIT" in c for c in project.get("classifiers", []))


def test_licence_text_is_the_verbatim_apache_2_0() -> None:
    text = (REPO / "LICENSE").read_text()
    assert text.lstrip().startswith("Apache License")
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text
    assert "END OF TERMS AND CONDITIONS" in text
    assert "http://www.apache.org/licenses/LICENSE-2.0" in text


def test_pep639_licence_metadata_ships_licence_and_notice() -> None:
    pyproject = _pyproject()
    assert pyproject["project"]["license-files"] == ["LICENSE", "NOTICE"]
    assert pyproject["project"]["authors"] == [{"name": "LINC Innovations"}]
    # PEP 639 `license = "<SPDX>"` strings need setuptools >= 77.
    assert "setuptools>=77.0" in pyproject["build-system"]["requires"]


def test_notice_credits_scrcpy() -> None:
    notice = (REPO / "NOTICE").read_text()
    assert "scrcpy" in notice and "Apache License, Version 2.0" in notice


def test_notice_names_the_vendored_binary_that_actually_ships() -> None:
    notice = (REPO / "NOTICE").read_text()
    vendored = "src/linc_nerve/vendor/scrcpy-server-v3.3.4"
    assert vendored in notice
    rest = vendored.removeprefix("src/linc_nerve/")
    repo_target = REPO / vendored
    target = repo_target if repo_target.is_file() else Path(linc_nerve.__file__).parent / rest
    assert target.is_file()


def test_readme_states_mirror_model_trust_stance_and_cta() -> None:
    readme = _readme()
    assert "release mirror" in readme
    assert "no telemetry" in readme.lower()
    assert "NERVE_USAGE_LOG" in readme
    assert "utm_source=github" in readme


def test_readme_documents_every_usage_log_mode() -> None:
    readme = _readme()
    assert "NERVE_USAGE_LOG=off|redacted|full" in readme
    for mode in ("`off`", "`redacted`", "`full`"):
        assert mode in readme, f"README does not document NERVE_USAGE_LOG mode {mode}"
    assert "LINC_HOME" in readme


def test_readme_quickstart_block_exists() -> None:
    readme = _readme()
    block = re.search(r"<!-- quickstart:start -->(.*?)<!-- quickstart:end -->", readme, re.S)
    assert block, "README must fence the quickstart so smoke.yml can run it verbatim"
    assert "uvx linc-nerve doctor" in block.group(1)


def test_readme_quickstart_runs_android_emulator_first() -> None:
    block = re.search(
        r"<!-- quickstart:start -->(.*?)<!-- quickstart:end -->", _readme(), re.S
    ).group(1)
    for step in (
        "emulator -avd nerve -no-window",
        "uvx linc-nerve discover",
        "uvx linc-nerve state --device emulator-5554",
        "claude mcp add nerve -- uvx linc-nerve bellows",
    ):
        assert step in block, f"quickstart is missing step: {step}"
    assert block.index("emulator -avd nerve") < block.index("uvx linc-nerve doctor")


def test_readme_quickstart_offers_pip_install() -> None:
    block = re.search(r"<!-- quickstart:start -->(.*?)<!-- quickstart:end -->", _readme(), re.S)
    assert block, "README must fence the quickstart"
    qs = block.group(1)
    assert "pipx install --python python3.13 linc-nerve" in qs
    assert "nerve discover" in qs


def test_uvx_entry_point_matches_package_name() -> None:
    """`uvx linc-nerve ...` runs the console script named after the package."""
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["linc-nerve"] == "linc_nerve.cli.main:main"
    assert scripts["nerve"] == "linc_nerve.cli.main:main"


def test_readme_field_note_code_links_resolve() -> None:
    """No README claim without a source: every linked source file exists."""
    linked = set(
        re.findall(rf"\]\({re.escape(PUBLIC_BLOB)}(src/linc_nerve/[\w/]+\.py)\)", _readme())
    )
    assert {
        "src/linc_nerve/core/adb_health.py",
        "src/linc_nerve/core/android_ui.py",
        "src/linc_nerve/core/device_state.py",
        "src/linc_nerve/mcp/_core.py",
    } <= linked
    for rel in linked:
        rest = rel.removeprefix("src/linc_nerve/")
        target = REPO / rel if (REPO / rel).is_file() else Path(linc_nerve.__file__).parent / rest
        assert target.is_file(), f"README links a missing file: {rel}"


def test_readme_section_order_puts_field_notes_before_quickstart() -> None:
    readme = _readme()
    order = [
        "## Field notes",
        "<!-- quickstart:start -->",
        "## iOS (experimental)",
        "## Trust",
        "## How this repo works",
        "Built by [LINC Innovations]",
    ]
    positions = [readme.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_contributing_requires_dco_and_security_has_contact() -> None:
    assert "Signed-off-by" in (REPO / "CONTRIBUTING.md").read_text()
    assert "Report a vulnerability" in (REPO / "SECURITY.md").read_text()


def test_security_replaces_the_review_stub_with_review_summaries_and_hashes() -> None:
    security = (REPO / "SECURITY.md").read_text()
    assert "## Independent review" in security
    assert "NERVE_ALLOWED_HOSTS" in security and "NERVE_ALLOWED_ORIGINS" in security
    stub = "(The adversarial review summary is added here by the release runbook before v0.1.0.)"
    assert stub not in security
    review_section = security.split("## Independent review", 1)[1]
    assert "2026-10-02" in review_section
    assert "2026-10-10" in review_section
    sha256_hashes = re.findall(r"\b[0-9a-f]{64}\b", review_section)
    assert len(sha256_hashes) >= 2


def test_security_release_candidate_verification_names_public_tag_and_commit() -> None:
    security = (REPO / "SECURITY.md").read_text()
    assert "to be exported" not in security
    assert "attached to the v0.1.0 release" not in security
    assert "### Release candidate verification" in security
    rc_section = security.split("### Release candidate verification", 1)[1]
    assert "LincForge/nerve" in rc_section
    assert "v0.1.0" in rc_section
    commit_match = re.search(r"commit `([0-9a-f]{12,40})`", rc_section)
    assert commit_match is not None
    # Published v0.1.0 identifiers (tag object on LincForge/nerve, PyPI wheel digest).
    assert "547786321703b7c8640e3f8a610e34f561fe7c9e" in rc_section
    assert "321e50e2aba0e668107b364a94852ee7796b42c1" in rc_section
    assert "747798cf8c869e4e0f6d49426faada97221cfd55e70fef540afa3fe2d949ca19" in rc_section
    # The pre-publication placeholder sentence is gone, the timestamp explanation stays.
    assert "After v0.1.0 is published" not in rc_section
    assert "records each file's timestamp" in rc_section
    assert "pip download linc-nerve==0.1.0" in rc_section


def test_changelog_has_first_public_release_section() -> None:
    assert re.search(r"^## \[0\.1\.0\]", (REPO / "CHANGELOG.md").read_text(), re.M)


def test_changelog_0_1_0_is_the_single_first_public_release() -> None:
    changelog = (REPO / "CHANGELOG.md").read_text()
    headings = re.findall(r"^## \[(.+?)\].*$", changelog, re.M)
    releases = [h for h in headings if re.fullmatch(r"\d+\.\d+\.\d+", h)]
    assert headings[0] == "Unreleased"
    # Later releases stack above it; 0.1.0 stays the single, oldest public release.
    assert releases[-1] == "0.1.0"
    assert headings.count("0.1.0") == 1
    section = changelog.split("## [0.1.0]", 1)[1].split("\n## ", 1)[0]
    assert "First public release." in section
    assert "Apache-2.0" in section


def _readme_link_targets() -> list[str]:
    return [a or b for a, b in _LINK_TARGET.findall(_readme())]


def test_readme_links_are_all_absolute_https() -> None:
    """The README is also the PyPI project page, where relative links and anchors 404."""
    targets = _readme_link_targets()
    assert targets, "no links found in README"
    relative = [t for t in targets if not t.startswith("https://")]
    assert relative == [], f"README has non-absolute links: {relative}"


def test_readme_repo_links_point_at_the_public_repo_and_resolve() -> None:
    """Links into this repo use the public `nerve` repo and name files that exist."""
    targets = _readme_link_targets()
    assert not [t for t in targets if "github.com/LincForge/linc-" in t]
    repo_paths = [t.removeprefix(PUBLIC_BLOB) for t in targets if t.startswith(PUBLIC_BLOB)]
    assert {"SECURITY.md", "CONTRIBUTING.md", "LICENSE", "NOTICE"} <= set(repo_paths)
    for rel in repo_paths:
        rest = rel.removeprefix("src/linc_nerve/")
        target = REPO / rel if (REPO / rel).is_file() else Path(linc_nerve.__file__).parent / rest
        assert target.is_file(), f"README links a missing file: {rel}"


def test_pyproject_project_urls_point_at_the_public_repo() -> None:
    urls = _pyproject()["project"]["urls"]
    assert urls["Homepage"] == PUBLIC_REPO
    assert urls["Source"] == PUBLIC_REPO
    assert urls["Issues"] == f"{PUBLIC_REPO}/issues"


def test_readme_cross_links_sibling_public_repos() -> None:
    targets = set(_readme_link_targets())
    assert "https://github.com/LincForge/crucible" in targets
    assert "https://github.com/LincForge/spectra" in targets
    assert "https://github.com/LincForge/mobile-agent-bench" in targets


_GLOBAL_FLAGS_NO_VAL = frozenset({"--json", "--human", "--stream", "--version", "--help", "-h"})
_GLOBAL_FLAGS_WITH_VAL = frozenset({"--output", "-o", "--timeout", "--log-level"})
_CLI_CMD_TOKEN = re.compile(r"\A[a-z][a-z0-9-]*\Z")


def _subcommand_from_tokens(tokens: list[str]) -> str | None:
    """Return the first CLI subcommand token after global options, or None if none/placeholder."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _GLOBAL_FLAGS_WITH_VAL:
            i += 2
            continue
        if any(tok.startswith(f"{flag}=") for flag in _GLOBAL_FLAGS_WITH_VAL):
            i += 1
            continue
        if tok in _GLOBAL_FLAGS_NO_VAL:
            i += 1
            continue
        if tok.startswith("<") or tok.startswith("["):
            return None
        if _CLI_CMD_TOKEN.fullmatch(tok):
            return tok
        return None
    return None


def _tokenize_snippet(snippet: str) -> list[str]:
    try:
        return shlex.split(snippet)
    except ValueError:
        return snippet.split()


def _commands_from_command_line(text: str) -> list[str]:
    """Extract subcommands from a shell-style command line or inline code snippet."""
    found: list[str] = []
    # Split shell pipelines/chains and `claude mcp add ... -- <cmd>` delimiters.
    for segment in re.split(r"(?:&&|\|\||[;\n|]|(?<=\s)--(?=\s))", text):
        tokens = _tokenize_snippet(segment.strip())
        # Skip leading VAR=val assignments.
        while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
            tokens = tokens[1:]
        if not tokens:
            continue
        if tokens[0] == "uvx" and len(tokens) >= 2 and tokens[1] == "linc-nerve":
            rest = tokens[2:]
        elif tokens[0] in ("nerve", "linc-nerve"):
            rest = tokens[1:]
        else:
            continue
        cmd = _subcommand_from_tokens(rest)
        if cmd is not None:
            found.append(cmd)
    return found


def _extract_readme_cli_commands(readme: str) -> list[str]:
    """Extract every CLI subcommand named with `nerve` / `linc-nerve` in README text."""
    commands: list[str] = []
    fence_re = re.compile(r"```([^\n]*)\n(.*?)```", re.S)
    pos = 0
    for match in fence_re.finditer(readme):
        prose = readme[pos : match.start()]
        for inline in re.findall(r"`([^`\n]+)`", prose):
            commands.extend(_commands_from_command_line(inline))
        lang = match.group(1).strip().lower()
        if lang in ("", "bash", "sh", "shell", "zsh"):
            for line in match.group(2).splitlines():
                stripped = line.strip()
                if stripped.startswith("$ "):
                    stripped = stripped[2:].strip()
                if not stripped or stripped.startswith("#"):
                    continue
                commands.extend(_commands_from_command_line(stripped))
        pos = match.end()
    tail = readme[pos:]
    for inline in re.findall(r"`([^`\n]+)`", tail):
        commands.extend(_commands_from_command_line(inline))
    return commands


def test_readme_cli_commands_exist_in_schema() -> None:
    """[contract] Every CLI command named in README.md exists in `nerve schema --json`."""
    result = CliRunner().invoke(main, ["schema", "--json"])
    assert result.exit_code == 0, result.stderr or result.output
    schema_commands = set(json.loads(result.output)["commands"].keys())

    named = _extract_readme_cli_commands(_readme())
    assert named, "expected CLI commands in README.md"
    missing = sorted(set(named) - schema_commands)
    assert missing == [], f"README.md names CLI command(s) absent from `nerve schema`: {missing}"


def test_readme_cli_command_extractor_flags_nonexistent_devices_command() -> None:
    """[contract] Extractor flags nonexistent commands like `nerve devices`."""
    sample = (
        "Run `uvx linc-nerve devices` or `nerve --human discover` or `nerve --version`.\n"
        "Placeholder `nerve <tool>` and `emulator -avd nerve -no-window`.\n"
        "```bash\nclaude mcp add nerve -- uvx linc-nerve bellows\n```\n"
    )
    assert _extract_readme_cli_commands(sample) == ["devices", "discover", "bellows"]


def test_readme_r6_stranger_run_fixes() -> None:
    """[contract] README reflects R6 stranger-run fixes accurately."""
    readme = _readme()
    assert "DEVICE_NOT_FOUND" in readme
    assert "nerve-output-<random>/<serial>_<NNNN>_<tool>.png" in readme
    assert "<temp dir>/nerve-output/…png" not in readme
    assert "com.linc.nerve-daemon" not in readme
    assert "post_merge_kickstart.py" not in readme
    assert "matches only by itself" not in readme
    assert "on every transport once identity is resolved" in readme.lower()
    assert "network transports with unknown identity" in readme.lower()
    assert "unreadable" in readme.lower() and "warn" in readme.lower()
    assert "positional" in readme and "--device" in readme
    assert "With several devices attached, `devices` lists" not in readme


def test_changelog_documents_r4_r6_molecule_after_0_2_0() -> None:
    """[contract] CHANGELOG documents every R4/R6 fix (and cli-kit 0.3.1) above [0.2.0]."""
    changelog = (REPO / "CHANGELOG.md").read_text()
    headings = re.findall(r"^## \[(.+?)\].*$", changelog, re.M)
    assert headings[0] == "Unreleased"
    assert "## [0.3.0] - 2026-11-19" in changelog
    since_0_2_0 = changelog.split("## [0.2.0]", 1)[0]
    for marker in (
        "linc-nerve-eh10",
        "linc-nerve-56eg",
        "linc-nerve-wp3e",
        "linc-nerve-hih6",
        "linc-nerve-vv14",
        "linc-nerve-3d1y",
        "linc-cli-kit",
        "0.3.1",
    ):
        assert marker in since_0_2_0, f"CHANGELOG since 0.2.0 missing {marker}"
