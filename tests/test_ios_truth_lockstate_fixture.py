"""Tests for physical iOS devicectl lockState fixtures (linc-nerve-pt4w)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import linc_nerve.core.ios  # noqa: F401 — ensure ios loads before conftest imports ios_discovery

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
OBSERVED_PATH = FIXTURES_DIR / "devicectl_lockstate_observed.json"
DERIVED_PATH = FIXTURES_DIR / "devicectl_lockstate_derived.json"
README_PATH = FIXTURES_DIR / "devicectl_lockstate_README.md"

DOCUMENTED_KEY_PATHS: tuple[tuple[str, ...], ...] = (
    ("result", "passcodeRequired"),
    ("result", "unlockedSinceBoot"),
)


def _get_key_path(data: dict[str, object], path: tuple[str, ...]) -> object:
    current: object = data
    for key in path:
        assert isinstance(current, dict), f"Expected dict at {key!r}, got {type(current).__name__}"
        assert key in current, f"Missing key {key!r} along path {'.'.join(path)}"
        current = current[key]
    return current


def test_lockstate_fixtures_load_and_differ_on_documented_booleans() -> None:
    """Both fixtures load as JSON objects and differ on the documented booleans."""
    observed = json.loads(OBSERVED_PATH.read_text(encoding="utf-8"))
    derived = json.loads(DERIVED_PATH.read_text(encoding="utf-8"))

    assert isinstance(observed, dict)
    assert isinstance(derived, dict)

    for key_path in DOCUMENTED_KEY_PATHS:
        obs_val = _get_key_path(observed, key_path)
        der_val = _get_key_path(derived, key_path)
        assert type(obs_val) is bool, f"Expected bool at {'.'.join(key_path)} in observed"
        assert type(der_val) is bool, f"Expected bool at {'.'.join(key_path)} in derived"
        assert obs_val != der_val, f"Expected differing booleans at {'.'.join(key_path)}"

    assert observed["result"]["passcodeRequired"] is False
    assert observed["result"]["unlockedSinceBoot"] is True
    assert derived["result"]["passcodeRequired"] is True
    assert derived["result"]["unlockedSinceBoot"] is False

    # Verify derived differs ONLY in the lock-state booleans.
    flipped = copy.deepcopy(observed)
    flipped["result"]["passcodeRequired"] = not observed["result"]["passcodeRequired"]
    flipped["result"]["unlockedSinceBoot"] = not observed["result"]["unlockedSinceBoot"]
    assert derived == flipped


def test_lockstate_readme_documents_capture_and_derived_fixture() -> None:
    """README records Xcode/iOS versions, state, key path, exit code, and derived status."""
    readme = README_PATH.read_text(encoding="utf-8")

    for required in (
        "Xcode 27.2",
        "26.6.1",
        "unlocked",
        "result.passcodeRequired",
        "result.unlockedSinceBoot",
        "devicectl_lockstate_observed.json",
        "devicectl_lockstate_derived.json",
        "derived",
    ):
        assert required in readme, f"Missing {required!r} in {README_PATH.name}"
