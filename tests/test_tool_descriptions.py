"""Pinned literal substring checks on MCP tool description contracts.

linc-nerve#99: mobile-agent-bench (45 runs) showed agents drive perception
visually-first — ``nerve_screenshot`` was called 213x, nearly as often as
``nerve_tap`` (227x), even on tasks the element tree answers directly. These
tests pin the tool docstrings (which FastMCP surfaces verbatim as MCP tool
descriptions via ``@wraps`` in ``_core.device_tool``) so that element-tree
perception (``nerve_state`` / ``nerve_elements`` / ``nerve_diff``) is
positioned as the PRIMARY way to observe the screen, and ``nerve_screenshot``
is positioned as the visual fallback that steers structural queries toward
the element-tree tools instead.

Do not weaken these assertions to make a description change pass — if a
docstring edit breaks one of these, the guidance regressed.
"""

from __future__ import annotations

from linc_nerve.mcp.screenshot_tools import _nerve_screenshot_sync
from linc_nerve.mcp.ui_tools import (
    _nerve_diff_sync,
    _nerve_elements_sync,
    _nerve_state_sync,
)

_PRIMARY_WORDS = ("primary", "first")


def _doc(fn) -> str:
    assert fn.__doc__, f"{fn.__name__} has no docstring"
    return fn.__doc__


class TestElementTreeIsPrimary:
    """nerve_state / nerve_elements / nerve_diff must claim primacy."""

    def test_nerve_state_claims_primary_or_first(self) -> None:
        doc = _doc(_nerve_state_sync).lower()
        assert any(w in doc for w in _PRIMARY_WORDS), (
            "nerve_state description must contain 'primary' or 'first' to "
            "steer agents to element-tree perception before screenshots"
        )

    def test_nerve_elements_claims_primary_or_first(self) -> None:
        doc = _doc(_nerve_elements_sync).lower()
        assert any(w in doc for w in _PRIMARY_WORDS), (
            "nerve_elements description must contain 'primary' or 'first' to "
            "steer agents to element-tree perception before screenshots"
        )

    def test_nerve_diff_claims_primary_or_first(self) -> None:
        doc = _doc(_nerve_diff_sync).lower()
        assert any(w in doc for w in _PRIMARY_WORDS), (
            "nerve_diff description must contain 'primary' or 'first' to "
            "steer agents to element-tree perception before screenshots"
        )


class TestScreenshotIsFallback:
    """nerve_screenshot must steer structural queries to the tree tools."""

    def test_nerve_screenshot_points_to_nerve_state(self) -> None:
        doc = _doc(_nerve_screenshot_sync)
        assert "nerve_state" in doc, (
            "nerve_screenshot description must point structural queries at nerve_state"
        )

    def test_nerve_screenshot_points_to_nerve_elements(self) -> None:
        doc = _doc(_nerve_screenshot_sync)
        assert "nerve_elements" in doc, (
            "nerve_screenshot description must point structural queries at nerve_elements"
        )

    def test_nerve_screenshot_declares_itself_the_fallback(self) -> None:
        doc = _doc(_nerve_screenshot_sync).lower()
        assert "fallback" in doc, (
            "nerve_screenshot description must explicitly call itself the "
            "visual fallback (element-tree perception is primary)"
        )


# ---------------------------------------------------------------------------
# Component B of spec 2026-07-16-agent-token-efficiency (program cc766c43):
# routing-critical pins per tool. Terms come from USE FOR / NOT FOR clauses
# and cross-referenced tool names. The trim (same PR, later commit) may cut
# any prose EXCEPT these — pins protect the 192e622/79f596e0 BM25 lessons.
# ---------------------------------------------------------------------------

KEYWORD_PINS: dict[str, list[str]] = {
    "_nerve_screenshot_sync": [
        "nerve_state",
        "nerve_elements",
        "nerve_stream_screenshot",
        "nerve_record_start",
        "fallback",
        "golden",
        "visual",
        "pixels",
        "one-off",
        "stateless",
        "png",
    ],
    "_nerve_state_sync": [
        "nerve_screenshot",
        "nerve_elements",
        "nerve_diff",
        "primary",
        "screenshot",
        "elements",
        "foreground",
        "elements_error",
    ],
    "_nerve_elements_sync": [
        "nerve_state",
        "nerve_diff",
        "nerve_screenshot",
        "primary",
        "clickable",
        "element tree",
        "tokens",
        "assertions",
    ],
    "_nerve_diff_sync": [
        "nerve_state",
        "primary",
        "poll",
        "delta",
        "baseline",
    ],
    "_nerve_stream_start_sync": [
        "nerve_stream_screenshot",
        "nerve_record_start",
        "nerve_record_fixed",
        "nerve_screenshot",
        "scrcpy",
        "stream",
        "mirror",
        "warm",
    ],
    "_nerve_stream_screenshot_sync": [
        "nerve_stream_status",
        "nerve_screenshot",
        "nerve_record_start",
        "stream",
        "frame",
        "inline",
    ],
    "_nerve_device_state_sync": [
        "nerve_key",
        "nerve_type",
        "nerve_inspect",
        "doze",
        "standby",
        "battery-saver",
        "whitelist",
        "screen",
        "read-back",
        "doze_force",
        "battery_unplug",
    ],
    "_nerve_inspect_sync": [
        "spectra_attach",
        "spectra_step",
        "spectra_detach",
        "nerve_logcat",
        "crash",
        "pid",
        "rss",
        "crash_signature",
        "post-mortem",
        "non-invasive",
        "debugger",
    ],
    "_nerve_swipe_sync": [
        "nerve_tap",
        "nerve_type",
        "nerve_navigate",
        "nerve_key",
        "nerve_long_press",
        "swipe",
        "drag",
        "scroll",
        "gesture",
        "notification shade",
    ],
    "_nerve_type_sync": [
        "nerve_tap",
        "nerve_swipe",
        "nerve_key",
        "type",
        "text",
        "keyboard",
        "focus",
    ],
    "_nerve_record_fixed_sync": [
        "nerve_record_start",
        "nerve_record_stop",
        "nerve_stream_start",
        "nerve_screenshot",
        "mp4",
        "fixed-length",
        "duration",
        "self-terminating",
    ],
    "_nerve_record_start_sync": [
        "nerve_record_stop",
        "nerve_stream_start",
        "nerve_record_fixed",
        "nerve_screenshot",
        "mp4",
        "open-ended",
        "recording",
        "artifact",
    ],
    "_nerve_record_stop_sync": [
        "nerve_record_start",
        "nerve_stream_stop",
        "mp4",
        "finalise",
        "file path",
        "pull",
    ],
    "_nerve_long_press_sync": [
        "long_press_timeout",
        "exceed",
        "1200",
        "samsung",
        "nerve_tap",
        "nerve_swipe",
    ],
}

# 900 chars ≈ 225 tokens — the trim target. Enforced by the trim commit;
# xfail until then so the pin commit lands green on the untrimmed text.
DESCRIPTION_BUDGET_CHARS = 900


def _resolve(fn_name: str):
    import importlib

    for mod in (
        "screenshot_tools",
        "device_state_tools",
        "diagnostic_tools",
        "input_tools",
        "recording_tools",
        "ui_tools",
    ):
        m = importlib.import_module(f"linc_nerve.mcp.{mod}")
        if hasattr(m, fn_name):
            return getattr(m, fn_name)
    raise AssertionError(f"tool fn {fn_name} not found")


class TestKeywordPins:
    def test_every_pin_table_entry_is_populated(self) -> None:
        empty = [k for k, v in KEYWORD_PINS.items() if not v]
        assert not empty, f"pin lists must be populated before the trim: {empty}"

    def test_routing_keywords_survive(self) -> None:
        missing: list[str] = []
        for fn_name, pins in KEYWORD_PINS.items():
            doc = (_resolve(fn_name).__doc__ or "").lower()
            missing.extend(f"{fn_name}: {pin!r}" for pin in pins if pin.lower() not in doc)
        assert not missing, f"routing-critical keywords missing: {missing}"


class TestDescriptionBudget:
    def test_top12_descriptions_fit_budget(self) -> None:
        over = {
            fn_name: len(_resolve(fn_name).__doc__ or "")
            for fn_name in KEYWORD_PINS
            if len(_resolve(fn_name).__doc__ or "") > DESCRIPTION_BUDGET_CHARS
        }
        assert not over, f"descriptions over {DESCRIPTION_BUDGET_CHARS} chars: {over}"


class TestLongPressTimeoutGuidance:
    """reactor cfaef523: Samsung's 500ms long_press_timeout default TIES a
    500ms hold and resolves it as a click — the TIP must not claim the
    500ms default works for most Android devices."""

    def test_does_not_claim_default_works_for_most_android(self) -> None:
        doc = _resolve("_nerve_long_press_sync").__doc__ or ""
        assert "works for most android" not in doc.lower(), (
            "long_press TIP must not claim 500ms works for most Android — "
            "Samsung factory defaults tie the 500ms hold and it resolves as a click"
        )
