"""Tests for nerve_logcat inline limits (250 lines / 24 KB) and tail_lines (linc-nerve-g92x)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.ios import IOSController
from linc_nerve.mcp import _core
from linc_nerve.mcp import diagnostic_tools as dt
from linc_nerve.mcp import server as mcp_mod


def _reset_singletons() -> None:
    if mcp_mod._device_mgr is not None:
        mcp_mod._device_mgr.close()
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = []
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    mcp_mod._android = mock_android
    mcp_mod._ios = mock_ios
    mcp_mod._state_mgr = None
    mcp_mod._metrics = None
    mcp_mod._device_mgr = None
    mcp_mod._emulator = None
    mcp_mod._cached_devices = []
    mcp_mod._cache_timestamp = 0.0


@pytest.fixture(autouse=True)
def _cleanup_singletons() -> Any:
    _reset_singletons()
    yield
    _reset_singletons()


def _setup_mock_android(logcat_output: str) -> MagicMock:
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = []
    mock_android.logcat.return_value = logcat_output
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    mcp_mod._android = mock_android
    mcp_mod._ios = mock_ios
    mcp_mod._cached_devices = [
        NerveDevice(
            serial="PIXEL8PRO",
            platform=Platform.ANDROID,
            device_type=DeviceType.PHONE,
            model="Pixel 8 Pro",
            os_version="14",
        )
    ]
    mcp_mod._cache_timestamp = time.monotonic()
    return mock_android


def test_200_short_lines_returned_inline_without_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_file = tmp_path / "should_not_exist.txt"
    monkeypatch.setattr(_core, "_next_output_path", lambda *a, **kw: str(out_file))

    lines = [f"10-14 08:00:00.{i:03d}  1000  1000 I TestTag: short line {i}" for i in range(200)]
    _setup_mock_android("\n".join(lines))

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", lines=200)
    assert result == {
        "lines": lines,
        "count": 200,
        "device": "PIXEL8PRO",
        "platform": "android",
    }
    assert not out_file.exists()


def test_250_short_lines_boundary_returned_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_file = tmp_path / "should_not_exist_250.txt"
    monkeypatch.setattr(_core, "_next_output_path", lambda *a, **kw: str(out_file))

    lines = [f"line-{i}" for i in range(250)]
    _setup_mock_android("\n".join(lines))

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", lines=250)
    assert result["lines"] == lines
    assert result["count"] == 250
    assert "logcat" not in result
    assert "tail_lines" not in result
    assert not out_file.exists()


def test_251_lines_spills_to_file_with_last_50_tail_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_file = tmp_path / "logcat_251.txt"
    monkeypatch.setattr(_core, "_next_output_path", lambda *a, **kw: str(out_file))

    lines = [f"10-14 08:00:00.{i:03d}  1000  1000 I TestTag: line {i}" for i in range(251)]
    _setup_mock_android("\n".join(lines))

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", lines=251)
    assert out_file.is_file()
    file_lines = out_file.read_text(encoding="utf-8").splitlines()
    assert file_lines == lines
    assert out_file.read_bytes() == "\n".join(lines).encode("utf-8")

    assert result == {
        "logcat": str(out_file),
        "count": 251,
        "tail_lines": file_lines[-50:],
        "tail_count": 50,
        "device": "PIXEL8PRO",
        "platform": "android",
        "hint": (
            f"Full output (251 lines) saved to {out_file}; "
            "tail_lines holds the last 50. "
            "Read the file only if you need earlier lines."
        ),
    }


def test_120_lines_of_400_chars_exceeding_24kb_spills_to_file_with_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_file = tmp_path / "logcat_over_24kb.txt"
    monkeypatch.setattr(_core, "_next_output_path", lambda *a, **kw: str(out_file))

    lines = [f"{i:03d}-" + ("x" * 396) for i in range(120)]
    assert all(len(line) == 400 for line in lines)
    assert sum(len(line.encode("utf-8")) for line in lines) == 48_000 > dt._LOGCAT_INLINE_MAX_BYTES

    _setup_mock_android("\n".join(lines))

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", lines=120)
    assert out_file.is_file()
    assert out_file.read_bytes() == "\n".join(lines).encode("utf-8")
    assert "lines" not in result
    assert result["logcat"] == str(out_file)
    assert result["count"] == 120
    assert result["tail_count"] == 50
    assert result["tail_lines"] == lines[-50:]
    assert result["hint"] == (
        f"Full output (120 lines) saved to {out_file}; "
        "tail_lines holds the last 50. "
        "Read the file only if you need earlier lines."
    )


def test_2000_char_line_in_tail_is_truncated_while_file_stays_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_file = tmp_path / "logcat_long_tail_line.txt"
    monkeypatch.setattr(_core, "_next_output_path", lambda *a, **kw: str(out_file))

    exact_500 = "E" * 500
    over_501 = "O" * 501
    long_line = "L" * 2000
    lines = [f"short-line-{i}" for i in range(248)] + [exact_500, over_501, long_line]
    assert len(lines) == 251

    _setup_mock_android("\n".join(lines))

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", lines=251)
    # File content is byte-identical to today's format (untruncated lines joined by \n).
    assert out_file.read_bytes() == "\n".join(lines).encode("utf-8")

    tail = result["tail_lines"]
    assert len(tail) == 50
    assert result["tail_count"] == 50
    # 500-char line is NOT cut; 501-char and 2000-char lines ARE cut at 500 chars + "…".
    assert tail[-3] == exact_500
    assert not tail[-3].endswith("…")
    assert tail[-2] == ("O" * 500) + "…"
    assert tail[-1] == ("L" * 500) + "…"


def test_byte_limit_exact_boundary_and_utf8_multibyte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_file = tmp_path / "logcat_utf8.txt"
    called_args: list[tuple[Any, ...]] = []

    def fake_next_path(device: str, prefix: str, ext: str = "png") -> str:
        called_args.append((device, prefix, ext))
        return str(out_file)

    monkeypatch.setattr(_core, "_next_output_path", fake_next_path)

    assert dt._LOGCAT_INLINE_MAX == 250
    assert dt._LOGCAT_INLINE_MAX_BYTES == 24 * 1024
    assert dt._LOGCAT_TAIL_LINES == 50
    assert dt._LOGCAT_TAIL_LINE_MAX_CHARS == 500

    # Exact 24 KB (24 lines * 1024 ASCII bytes = 24576 bytes) stays inline.
    exact_24kb = ["a" * 1024 for _ in range(24)]
    r_inline = dt._process_log_lines("PIXEL8PRO", "android", exact_24kb)
    assert r_inline["lines"] == exact_24kb
    assert not out_file.exists()

    # 24 KB + 1 byte spills.
    over_by_one = [*exact_24kb, "b"]
    r_spill = dt._process_log_lines("PIXEL8PRO", "android", over_by_one)
    assert r_spill["logcat"] == str(out_file)
    assert r_spill["tail_count"] == 25
    assert len(r_spill["tail_lines"]) == 25
    assert called_args == [("PIXEL8PRO", "logcat", "txt")]

    # Multi-byte UTF-8: 100 lines of 100 "é" chars (10,000 chars, 20,000 UTF-8 bytes) -> inline;
    # 130 lines of 100 "é" chars (13,000 chars <= 24576, but 26,000 UTF-8 bytes > 24576) -> spills.
    utf8_inline = ["é" * 100 for _ in range(100)]
    assert "lines" in dt._process_log_lines("PIXEL8PRO", "android", utf8_inline)

    utf8_spill = ["é" * 100 for _ in range(130)]
    r_utf8_spill = dt._process_log_lines("PIXEL8PRO", "android", utf8_spill)
    assert r_utf8_spill["logcat"] == str(out_file)
    assert r_utf8_spill["tail_count"] == 50
