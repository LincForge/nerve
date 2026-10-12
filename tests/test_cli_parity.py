"""Conformance: every nerve MCP tool is a CLI command under the kit's output contract."""

from __future__ import annotations

import pytest
from linc_cli_kit.testing import assert_cli_parity

from linc_nerve.cli.main import _registry_server, main

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")

# Hand-written overrides need a valid invocation so the conformance suite can drive their
# success/error stub checks (testing.py "fail closed" rule for overrides).
_OVERRIDE_SAMPLES = {
    "nerve_discover": {},
    "nerve_screenshot": {"device": "X"},
    "nerve_launch": {"device": "X", "package": "com.x"},
    "nerve_kill": {"device": "X", "package": "com.x"},
    "nerve_elements": {"device": "X"},
    "nerve_tap": {"device": "X", "x": 1, "y": 1},
}


def test_cli_parity() -> None:
    report = assert_cli_parity(
        main, _registry_server(), prefix="nerve_", samples=_OVERRIDE_SAMPLES
    )
    assert report.tools == 48
    assert report.skipped == 0
