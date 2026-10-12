"""Precise error mapping for the nerve CLI transport (linc-nerve-zqy.4).

linc-cli-kit's ``errors.classify`` treats every tool-result error dict as a generic
``TOOL_ERROR`` / ``internal`` / non-retryable outcome with no hint — that's accurate for a
truly internal failure, but dishonest for the handful of well-known nerve shapes (unknown
device serial, a device reserved by another session, an operation unsupported on this device
type). This module recognises those shapes and raises a precise ``linc_cli_kit.ToolError``
*before* the kit's ``emit``/``classify`` ever sees the dict — see
``linc_nerve.cli.main.NerveTransport``.

Pure and side-effect free: no imports of nerve controllers, no I/O.
"""

from __future__ import annotations

import re

from linc_cli_kit import ToolError

_DEVICE_NOT_FOUND_RE = re.compile(r"Device '([^']+)' not found")
_MCP_TOOL_NAME_RE = re.compile(r"\bnerve_[a-z0-9_]+\b")

_DISCOVER_HINT = "Run `nerve discover` to list devices."
_RESERVED_HINT = "Wait for the lease to expire or check the holder with `nerve pool`."


def cli_hint(text: str) -> str:
    """Rewrite MCP tool names (``nerve_discover``) to CLI commands (``nerve discover``).

    Strips the ``nerve_`` prefix and turns the remaining underscores into hyphens, e.g.
    ``nerve_clear_app_data`` -> ``nerve clear-app-data``.
    """

    def _rewrite(match: re.Match[str]) -> str:
        name = match.group(0)
        command = name[len("nerve_") :].replace("_", "-")
        return f"nerve {command}"

    return _MCP_TOOL_NAME_RE.sub(_rewrite, text)


def _device_not_found_error(message: str) -> ToolError | None:
    match = _DEVICE_NOT_FOUND_RE.search(message)
    if match is None:
        return None
    return ToolError(
        message,
        code="DEVICE_NOT_FOUND",
        category="not_found",
        retryable=False,
        exit_code=2,
        hint=_DISCOVER_HINT,
        details={"device": match.group(1)},
    )


def honest_error(result: object) -> ToolError | None:
    """Map a nerve tool-result error dict to a precise ToolError, or None for the kit default.

    Returns ``None`` unless ``result`` is a dict with a truthy ``error`` key — mirroring the
    fleet convention that an ``error`` key present but falsy (``None``/``""``) means success.
    """
    if not isinstance(result, dict):
        return None
    message = result.get("error")
    if not message:
        return None
    message = str(message)

    not_found = _device_not_found_error(message)
    if not_found is not None:
        return not_found

    if "is reserved by another session" in message or " is held by " in message:
        return ToolError(
            message,
            code="DEVICE_RESERVED",
            category="unavailable",
            retryable=True,
            exit_code=1,
            hint=_RESERVED_HINT,
        )

    if result.get("unsupported") is True:
        return ToolError(
            message,
            code="UNSUPPORTED",
            category="validation",
            retryable=False,
            exit_code=2,
            hint=cli_hint(message),
        )

    return None


def honest_toolerror(exc: ToolError) -> ToolError:
    """Re-map a kit ToolError/UsageError hiding a device-not-found message to DEVICE_NOT_FOUND.

    Covers the daemon 400 -> ``UsageError`` path (e.g. ``recording_tools``' ``get_device_sync``
    ``ValueError`` becomes a daemon HTTP 400, which ``HttpDaemonTransport`` turns into a plain
    ``UsageError``). Any other exception is returned unchanged.
    """
    not_found = _device_not_found_error(str(exc))
    return not_found if not_found is not None else exc
