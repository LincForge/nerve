"""`nerve cleanup` — remove stale or idle `nerve-output-*` temp directories (linc-nerve-si3y).

The scanning/removal logic lives in :mod:`linc_nerve.core.output_retention` (shared with the
first-output sweep in ``mcp/_core``); this module is only the click command.
"""

from __future__ import annotations

import json
from typing import Any

import click
from linc_cli_kit import current_mode

from linc_nerve.core.output_retention import cleanup_output_dirs


def _render_text(payload: dict[str, Any]) -> str:
    removed = payload["removed"]
    skipped = payload["skipped"]
    lines = [
        f"Removed {len(removed)} dir(s), skipped {len(skipped)}, "
        f"freed {payload['bytes_freed']} bytes."
    ]
    lines.extend(f"  removed: {p}" for p in removed)
    lines.extend(f"  skipped: {p}" for p in skipped)
    return "\n".join(lines)


@click.command("cleanup")
@click.option(
    "--all",
    "remove_all",
    is_flag=True,
    help=(
        "Also remove nerve-output-* directories modified in the last 60 seconds. A directory "
        "whose nerve process is still running is always kept."
    ),
)
@click.option(
    "--human",
    "human_output",
    is_flag=True,
    help="Emit human-readable text instead of JSON.",
)
def cleanup(remove_all: bool, human_output: bool) -> None:
    """Remove stale or idle nerve-output-* directories from the temp root."""
    payload = cleanup_output_dirs(remove_all=remove_all)
    if human_output or current_mode() == "human":
        click.echo(_render_text(payload))
    else:
        click.echo(json.dumps(payload))
