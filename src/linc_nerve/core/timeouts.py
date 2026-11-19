"""Single source of truth for per-tool timeout budgets across nerve layers."""

from __future__ import annotations

import math

UNLISTED_MCP_CEILING_S: int = 90
UNLISTED_CLI_DEFAULT_S: int = 30
DAEMON_CEILING_S: float = 180.0

INNER_BUDGET_S: dict[str, int] = {
    "nerve_install": 120,
    "nerve_record_fixed": 150,
    "nerve_record_stop": 120,
    "nerve_emulator_start": 120,
}

MCP_CEILING_S: dict[str, int] = {
    "nerve_install": 150,
    "nerve_record_fixed": 160,
    "nerve_record_stop": 150,
    "nerve_emulator_start": 150,
}

CLI_DEFAULT_S: dict[str, int] = {
    "nerve_install": 200,
    "nerve_record_fixed": 200,
    "nerve_record_stop": 200,
    "nerve_emulator_start": 200,
}


def _require_positive_finite(value: int | float, label: str) -> int | float:
    """Raise ValueError unless *value* is a finite positive number (never bool)."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be a finite positive number, got {value!r}")
    return value


def inner_budget(tool_name: str) -> int | float:
    """Return the inner operation budget in seconds for a listed tool."""
    return _require_positive_finite(INNER_BUDGET_S[tool_name], f"INNER_BUDGET_S[{tool_name!r}]")


def mcp_ceiling(tool_name: str) -> int | float:
    """Return the stdio MCP wrapper ceiling in seconds (90s for unlisted tools)."""
    raw = MCP_CEILING_S.get(tool_name, UNLISTED_MCP_CEILING_S)
    return _require_positive_finite(raw, f"MCP_CEILING_S[{tool_name!r}]")


def cli_default(tool_name: str) -> int | float:
    """Return the CLI default timeout in seconds (30s for unlisted tools)."""
    raw = CLI_DEFAULT_S.get(tool_name, UNLISTED_CLI_DEFAULT_S)
    return _require_positive_finite(raw, f"CLI_DEFAULT_S[{tool_name!r}]")


def validate_timeouts_table() -> None:
    """Validate that all timeout table entries are positive finite numbers and ordered."""
    keys = set(INNER_BUDGET_S)
    if set(MCP_CEILING_S) != keys or set(CLI_DEFAULT_S) != keys:
        raise ValueError(
            "timeouts table keys must match across INNER_BUDGET_S, MCP_CEILING_S, CLI_DEFAULT_S"
        )
    daemon = _require_positive_finite(DAEMON_CEILING_S, "DAEMON_CEILING_S")
    for tool in keys:
        inner = inner_budget(tool)
        mcp = mcp_ceiling(tool)
        cli = cli_default(tool)
        if not (inner < mcp <= daemon < cli):
            raise ValueError(
                f"Expected inner < mcp <= daemon < cli for {tool}, "
                f"got {inner} < {mcp} <= {daemon} < {cli}"
            )


validate_timeouts_table()
