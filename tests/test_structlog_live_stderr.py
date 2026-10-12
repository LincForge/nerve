"""Regression: structlog must write to the CURRENT ``sys.stderr``, not the one that
existed when ``structlog.configure`` ran.

``PrintLoggerFactory(file=sys.stderr)`` captures the stream *object* at configure time,
and modules that bind a logger at import time keep that object for the life of the
process. Under pytest that object is the first session's capture stream. mutmut runs
pytest in-process twice (stats, then clean tests), so on the second run every log call
hit a closed stream: ``ValueError: I/O operation on closed file``. That turned most
diff-mutation runs on linc-nerve into INSTRUMENT_ERROR from 2026-10-01 on
(linc-ops#1121). The same failure hits any test that runs after another test closed
``sys.stderr``.
"""

from __future__ import annotations

import io
import sys

import structlog

from linc_nerve.live_stderr import LIVE_STDERR, configure_structlog_to_stderr


def test_live_stderr_writes_to_the_current_sys_stderr(monkeypatch) -> None:
    first, second = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    LIVE_STDERR.write("a")
    monkeypatch.setattr(sys, "stderr", second)
    LIVE_STDERR.write("b")
    LIVE_STDERR.flush()
    assert first.getvalue() == "a"
    assert second.getvalue() == "b"


def test_logger_bound_before_a_stream_swap_survives_the_old_stream_closing(
    monkeypatch,
) -> None:
    original = structlog.get_config()
    try:
        first = io.StringIO()
        monkeypatch.setattr(sys, "stderr", first)
        configure_structlog_to_stderr()
        log = structlog.get_logger().bind(probe="x")  # bound while `first` is live
        first.close()
        second = io.StringIO()
        monkeypatch.setattr(sys, "stderr", second)
        log.info("after_swap")
        assert "after_swap" in second.getvalue()
    finally:
        structlog.configure(**original)
