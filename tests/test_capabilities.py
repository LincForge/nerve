"""Tests for the platform-support capability matrix + nerve_capabilities tool.

The matrix is the agent-facing capability surface (issue #81): per-platform
support for the major capability groups, with explicit ``not_supported``
entries for known gaps (motion injection on iOS, watchOS WDA, physical-iOS
cold_start, iOS physical MJPEG). Every tool referenced by the matrix must be
a real
registered MCP tool — the registration decorators are the single source.
"""

from __future__ import annotations

import pytest

from linc_nerve.core.capabilities import (
    PLATFORMS,
    STATUS_NOT_SUPPORTED,
    STATUS_PARTIAL,
    STATUS_SUPPORTED,
    get_capabilities,
)

# Importing the server module imports every tool module, which populates the
# registration registry via the @device_tool/@global_tool decorators.
from linc_nerve.mcp import _core
from linc_nerve.mcp import server as mcp_mod  # noqa: F401  (import for side effects)

VALID_STATUSES = {STATUS_SUPPORTED, STATUS_PARTIAL, STATUS_NOT_SUPPORTED}


# ---------------------------------------------------------------------------
# Matrix shape
# ---------------------------------------------------------------------------


def test_platforms_enumeration() -> None:
    assert PLATFORMS == (
        "android",
        "android-watch",
        "ios-sim",
        "ios-physical",
        "watchos",
    )


def test_matrix_covers_all_platforms_per_group() -> None:
    caps = get_capabilities()["capabilities"]
    assert caps, "capability matrix must not be empty"
    for group, entry in caps.items():
        assert set(entry["support"].keys()) == set(PLATFORMS), (
            f"group {group!r} must declare support for every platform"
        )
        for platform, support in entry["support"].items():
            assert support["status"] in VALID_STATUSES, (
                f"{group}/{platform}: invalid status {support['status']!r}"
            )
            # Anything short of full support must say why.
            if support["status"] != STATUS_SUPPORTED:
                assert support.get("note"), (
                    f"{group}/{platform}: {support['status']} entries need a note"
                )


def test_matrix_has_major_capability_groups() -> None:
    caps = get_capabilities()["capabilities"]
    for group in (
        "discovery",
        "screenshot",
        "video_stream",
        "ui_elements",
        "input",
        "app_management",
        "cold_start",
        "sensor_injection",
        "screen_recording",
        "logs",
        "emulator_control",
    ):
        assert group in caps, f"missing major capability group {group!r}"


# ---------------------------------------------------------------------------
# Explicit not_supported entries (capability honesty — issue #81)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("group", "platform"),
    [
        ("sensor_injection", "ios-sim"),  # motion injection on iOS
        ("sensor_injection", "ios-physical"),
        ("ui_elements", "watchos"),  # watchOS WDA
        ("input", "watchos"),
        ("cold_start", "ios-physical"),  # no clean wipe primitive on physical iOS
        ("cold_start", "watchos"),
        ("video_stream", "ios-physical"),  # iOS physical MJPEG
    ],
)
def test_explicit_not_supported_entries(group: str, platform: str) -> None:
    caps = get_capabilities()["capabilities"]
    assert caps[group]["support"][platform]["status"] == STATUS_NOT_SUPPORTED


def test_cold_start_ios_sim_supported_with_needs_reinstall_caveat() -> None:
    """ios-sim cold_start is supported (uninstall-based, 047 Phase B2).

    The note must carry the contract keywords so an agent reading the matrix
    learns it must reinstall before the next launch.
    """
    entry = get_capabilities()["capabilities"]["cold_start"]["support"]["ios-sim"]
    assert entry["status"] == STATUS_SUPPORTED
    note = entry["note"].lower()
    assert "uninstall" in note
    assert "needs_reinstall" in note


# ---------------------------------------------------------------------------
# Single-sourcing against the registration decorators
# ---------------------------------------------------------------------------


def test_registry_populated_by_decorators() -> None:
    registry = _core.TOOL_REGISTRY
    assert "nerve_discover" in registry
    assert "nerve_capabilities" in registry
    assert registry["nerve_discover"]["kind"] == "global"
    assert registry["nerve_tap"]["kind"] == "device"
    for name, meta in registry.items():
        assert name.startswith("nerve_")
        assert meta["kind"] in {"device", "global"}
        assert meta["description"], f"{name} needs a docstring first line"


def test_every_matrix_tool_is_registered() -> None:
    registry = _core.TOOL_REGISTRY
    caps = get_capabilities()["capabilities"]
    for group, entry in caps.items():
        assert entry["tools"], f"group {group!r} must list its provider tools"
        for tool in entry["tools"]:
            assert tool in registry, f"group {group!r} references unregistered tool {tool!r}"


def test_tool_count_matches_registry() -> None:
    result = get_capabilities()
    assert result["tool_count"] == len(_core.TOOL_REGISTRY)


def test_tool_count_matches_fastmcp_server() -> None:
    """The registry must not drift from what FastMCP actually exposes."""
    import asyncio

    names = {t.name for t in asyncio.run(_core.server.list_tools())}
    assert names == set(_core.TOOL_REGISTRY.keys())


# ---------------------------------------------------------------------------
# The MCP tool itself
# ---------------------------------------------------------------------------


def test_nerve_capabilities_sync_returns_matrix() -> None:
    from linc_nerve.mcp.discovery_tools import _nerve_capabilities_sync

    result = _nerve_capabilities_sync()
    assert "error" not in result
    assert result["platforms"] == list(PLATFORMS)
    assert result["tool_count"] == len(_core.TOOL_REGISTRY)
    assert result["capabilities"] == get_capabilities()["capabilities"]
