"""A stderr sink for structlog that follows ``sys.stderr`` instead of pinning it.

``structlog.PrintLoggerFactory(file=sys.stderr)`` stores the stream object that is
``sys.stderr`` at configure time, and a logger bound at import time keeps it for the
life of the process. When something later replaces or closes that object (pytest's
capture between sessions, a stdio test, mutmut's second in-process pytest run), every
log call raises ``ValueError: I/O operation on closed file`` (linc-ops#1121).

Imports nothing from ``linc_nerve`` so it can run before any module binds a logger.
"""

from __future__ import annotations

import sys

import structlog


class _LiveStderr:
    """File-like proxy that resolves ``sys.stderr`` on every write."""

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


LIVE_STDERR = _LiveStderr()


def configure_structlog_to_stderr() -> None:
    """Point structlog at the live stderr. stdout is the MCP JSON-RPC transport and
    the CLI's JSON payload, so log records must never reach it."""
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=LIVE_STDERR))
