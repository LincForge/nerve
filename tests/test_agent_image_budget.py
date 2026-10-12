"""Agent-context image budget (spec 2026-07-16-agent-token-efficiency, rev 2).

The budget applies ONLY in the FastMCP wrapper layer (_core.device_tool) —
daemon and in-process callers must keep canonical bytes. See 01c1763d for why
that boundary matters.
"""

from __future__ import annotations

import io
import json
import os

from PIL import Image as PILImage

from linc_nerve.mcp._core import (
    _AGENT_MAX_PIXELS,
    _INLINE_IMAGE_KEY,
    _apply_agent_image_budget,
    _rescale_for_agent,
)


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), "purple").save(buf, format="PNG")
    return buf.getvalue()


class TestRescaleForAgent:
    def test_over_budget_image_is_scaled_to_pixel_budget(self) -> None:
        out = _rescale_for_agent(_png(488, 1080))  # 527k px > 300k budget
        img = PILImage.open(io.BytesIO(out))
        assert img.width * img.height <= _AGENT_MAX_PIXELS
        # Aspect ratio preserved within rounding
        assert abs(img.width / img.height - 488 / 1080) < 0.01
        assert img.format == "PNG"

    def test_under_budget_image_is_returned_unchanged(self) -> None:
        data = _png(300, 600)  # 180k px < 300k budget
        assert _rescale_for_agent(data) is data

    def test_non_image_bytes_pass_through(self) -> None:
        junk = b"not a png"
        assert _rescale_for_agent(junk) is junk


class TestApplyAgentImageBudget:
    def test_inline_bytes_are_rescaled(self) -> None:
        result = {"screenshot": "/nonexistent/x.png", _INLINE_IMAGE_KEY: _png(488, 1080)}
        out = _apply_agent_image_budget(result, {})
        img = PILImage.open(io.BytesIO(out[_INLINE_IMAGE_KEY]))
        assert img.width * img.height <= _AGENT_MAX_PIXELS

    def test_full_resolution_kwarg_exempts(self) -> None:
        raw = _png(488, 1080)
        result = {_INLINE_IMAGE_KEY: raw}
        out = _apply_agent_image_budget(result, {"full_resolution": True})
        assert out[_INLINE_IMAGE_KEY] is raw

    def test_annotate_kwarg_exempts(self) -> None:
        raw = _png(488, 1080)
        result = {_INLINE_IMAGE_KEY: raw}
        out = _apply_agent_image_budget(result, {"annotate": True})
        assert out[_INLINE_IMAGE_KEY] is raw

    def test_path_artifact_gets_agent_sibling_and_swap(self, tmp_path) -> None:
        canonical = tmp_path / "device_state_1.png"
        canonical.write_bytes(_png(488, 1080))
        result = {"screenshot": str(canonical), "device": "D"}
        out = _apply_agent_image_budget(result, {})
        assert out["screenshot"] == str(tmp_path / "device_state_1.agent.png")
        assert os.path.exists(out["screenshot"])
        with PILImage.open(out["screenshot"]) as img:
            assert img.width * img.height <= _AGENT_MAX_PIXELS
        # Canonical artifact untouched — daemon/crucible consumers depend on it
        with PILImage.open(canonical) as img_c:
            assert (img_c.width, img_c.height) == (488, 1080)

    def test_under_budget_path_artifact_is_left_alone(self, tmp_path) -> None:
        canonical = tmp_path / "small.png"
        canonical.write_bytes(_png(300, 600))
        result = {"screenshot": str(canonical)}
        out = _apply_agent_image_budget(result, {})
        assert out["screenshot"] == str(canonical)  # no sibling, no swap

    def test_missing_or_non_path_screenshot_is_ignored(self) -> None:
        for value in ("/nonexistent/y.png", "", None, 42):
            result = {"screenshot": value}
            out = _apply_agent_image_budget(result, {})
            assert out["screenshot"] == value

    def test_result_stays_json_serializable_after_budget(self, tmp_path) -> None:
        canonical = tmp_path / "s.png"
        canonical.write_bytes(_png(488, 1080))
        result = {"screenshot": str(canonical), "size_bytes": 3}
        out = _apply_agent_image_budget(result, {})
        json.dumps(out)  # must not raise

    def test_rescaled_inline_image_reports_agent_width_height(self) -> None:
        result = {"screenshot": "/nonexistent/x.png", _INLINE_IMAGE_KEY: _png(488, 1080)}
        out = _apply_agent_image_budget(result, {})
        img = PILImage.open(io.BytesIO(out[_INLINE_IMAGE_KEY]))
        assert (out["agent_width"], out["agent_height"]) == (img.width, img.height)

    def test_under_budget_result_gets_no_agent_keys(self) -> None:
        result = {"screenshot": "/nonexistent/x.png", _INLINE_IMAGE_KEY: _png(300, 600)}
        out = _apply_agent_image_budget(result, {})
        assert "agent_width" not in out
        assert "agent_height" not in out

    def test_full_resolution_call_gets_no_agent_keys(self) -> None:
        result = {"screenshot": "/nonexistent/x.png", _INLINE_IMAGE_KEY: _png(488, 1080)}
        out = _apply_agent_image_budget(result, {"full_resolution": True})
        assert "agent_width" not in out
        assert "agent_height" not in out


class TestStreamScreenshotInlineBudget:
    def test_inline_b64_branch_is_budgeted(self, monkeypatch) -> None:
        """inline=True returns base64 in JSON TEXT (~1 tok/3.5 chars) — a full
        frame is ~100k+ text tokens. The branch must apply the agent budget."""
        import base64

        from linc_nerve.mcp import _core, screenshot_tools

        big = _png(488, 1080)

        class FakeScrcpy:
            def screenshot(self, device):
                return big

        class FakeAndroid:
            def _get_scrcpy(self):
                return FakeScrcpy()

        monkeypatch.setattr(_core, "_ensure_initialized", lambda: (FakeAndroid(), None))
        monkeypatch.setattr(_core, "_resolve_device", lambda d: (object(), "android"))
        monkeypatch.setattr(_core, "_check_guard", lambda d: None)
        monkeypatch.setattr(_core, "_metrics", None)

        result = screenshot_tools._nerve_stream_screenshot_sync("D1", inline=True)
        data = base64.b64decode(result["screenshot_b64"])
        with PILImage.open(io.BytesIO(data)) as img:
            assert img.width * img.height <= _AGENT_MAX_PIXELS
