"""Conformance: every nerve MCP tool is a CLI command under the kit's output contract."""

from __future__ import annotations

import anyio
import pytest
from linc_cli_kit import SDKToolError, registry_of
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
    assert report.tools == 49
    assert report.skipped == 0


def test_strict_tools_contract() -> None:
    """[contract] Every linc-nerve MCP tool forbids extra properties and unknown arguments."""
    srv = _registry_server()
    specs = registry_of(srv)
    assert specs
    for spec in specs:
        assert spec.parameters.get("additionalProperties") is False, (
            f"{spec.name}: missing additionalProperties: false"
        )
    with pytest.raises(SDKToolError, match="unknown_arg_xyz"):
        anyio.run(srv.call_tool, "nerve_discover", {"unknown_arg_xyz": 1})


def test_strict_tools_contract_rejects_extra_arg_on_global_tool_before_it_runs() -> None:
    """[contract] mcp 2.x: an unknown argument to a no-arg global tool is a ToolError naming it.

    UnexpectedToolError subclasses ToolError on 2.x, so assert the exact class: argument
    validation is an anticipated failure whose text reaches the model.
    """
    srv = _registry_server()
    with pytest.raises(SDKToolError, match=r"\bx\b") as excinfo:
        anyio.run(srv.call_tool, "nerve_session_stats", {"x": 1})
    assert type(excinfo.value) is SDKToolError
    assert "extra" in str(excinfo.value).lower()
