"""Shared confirmation gate for destructive nerve tools (reactor c12557f2).

Deliberately does NOT import ``linc_nerve.mcp._core`` — ``_core`` registers
tools from modules that import this one, so an import back into ``_core``
would create a cycle. This module only needs stdlib types.
"""

from __future__ import annotations

from typing import Any

# Tools that mutate device/app state irreversibly (or destructively enough
# that an accidental call is costly) and therefore require an explicit
# ``confirm=True`` kwarg before touching the controller. Single source of
# truth for both the runtime gate (confirmation_gate) and the MCP
# destructiveHint annotation (mcp/_core.py device_tool/global_tool).
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {
        "nerve_uninstall",
        "nerve_clear_app_data",
        "nerve_grant_permission",
    }
)

# Tools that were reviewed for the same destructive-confirmation treatment
# and deliberately NOT gated, with the one-line reason recorded so the
# decision doesn't silently rot if the tool is renamed or revisited. Keys
# must stay disjoint from DESTRUCTIVE_TOOLS.
REVIEWED_NOT_GATED: dict[str, str] = {
    "nerve_device_state": (
        "State mutations are reversible via action=reset, and "
        "device-truth-bench's linc_bench runner/nerve_client call "
        "battery_unplug/screen_off/doze_force/standby_set/whitelist_remove "
        "without confirm — gating would break that consumer."
    ),
    "nerve_inject_sensor": "Android-emulator-only, reversible sensor values.",
    "nerve_kill": "Force-stop — the app is relaunchable, not destructive.",
}


def confirmation_gate(confirm: object) -> dict[str, Any] | None:
    """Return a confirmation-required error dict unless *confirm* is True.

    Returns a FRESH dict each call — callers may safely mutate the returned
    dict without affecting any other caller. Returns ``None`` when *confirm*
    is exactly ``True`` (not truthy — a stringly-typed ``"true"`` or ``1``
    must NOT pass), signaling the caller should proceed.
    """
    if confirm is not True:
        return {"success": False, "error": "confirmation_required"}
    return None
