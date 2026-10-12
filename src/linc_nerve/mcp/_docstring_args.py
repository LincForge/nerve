"""Google-style ``Args:`` docstring parsing for MCP tool parameter descriptions.

Each tool's ``Args:`` section is the single source of its parameter help: parsed once at
registration (``_core._register_tool``) and attached to the registered wrapper's signature as
``Annotated[<same type>, Field(description=...)]``. FastMCP then emits it as the JSON-schema
property ``description`` — which MCP clients show, and from which linc-cli-kit fills every
generated CLI option's ``--help`` (and ``nerve schema --json``). Types and defaults are never
touched, so the inputSchema changes only by the added ``description`` keys
(tests/test_schema_help.py pins that).
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

# ``name: text`` or ``name (type): text`` at the entry column.
_ENTRY = re.compile(r"(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _section_lines(doc: str | None) -> list[str]:
    """The non-blank body lines of the ``Args:`` section (empty if there is none)."""
    lines = inspect.cleandoc(doc or "").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "Args:"), None)
    if start is None:
        return []
    header = _indent(lines[start])
    body: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        if _indent(line) <= header:
            break  # next section (``Returns:`` ...) or dedented prose
        body.append(line)
    return body


def parse_args_section(doc: str | None) -> dict[str, str]:
    """Map each ``Args:`` entry name to its description, continuation lines joined."""
    body = _section_lines(doc)
    entries: dict[str, str] = {}
    column = _indent(body[0]) if body else 0
    current: str | None = None
    for line in body:
        match = _ENTRY.match(line.strip()) if _indent(line) == column else None
        if match:
            current = match.group(1)
            entries[current] = match.group(2).strip()
        elif current is not None:
            entries[current] = f"{entries[current]} {line.strip()}".strip()
    return entries


def described_signature(fn: Callable[..., Any]) -> inspect.Signature:
    """*fn*'s evaluated signature with each documented parameter's description attached.

    Undocumented or unannotated parameters are left exactly as they are, so FastMCP's own
    handling of them (and the generated schema) is unchanged.
    """
    sig = inspect.signature(fn, eval_str=True)
    descriptions = parse_args_section(fn.__doc__)
    params = [
        param.replace(annotation=Annotated[param.annotation, Field(description=text)])
        if (text := descriptions.get(name)) and param.annotation is not param.empty
        else param
        for name, param in sig.parameters.items()
    ]
    return sig.replace(parameters=params)
