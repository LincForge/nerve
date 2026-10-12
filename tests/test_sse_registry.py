import linc_nerve.mcp.sse_server as sse


def test_ensure_ready_in_http_tool_registry():
    # The HTTP daemon (:8111) scans _TOOL_MODULES to build its tool registry.
    # crucible/ops call nerve_ensure_ready over HTTP, so it MUST be reachable there.
    sse._TOOL_REGISTRY = None  # force a rebuild
    registry = sse._get_tool_registry()
    assert "nerve_ensure_ready" in registry


def test_state_tools_module_registered():
    assert "linc_nerve.mcp.state_tools" in sse._TOOL_MODULES
