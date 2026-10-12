"""Tests for screenshot annotation — bounding boxes + element IDs.

TDD RED phase: all tests written before any production code.
"""

from __future__ import annotations

import io

from PIL import Image

from linc_nerve.core.annotator import annotate_screenshot, annotate_screenshot_pil

# --- Test helpers ---


def _make_screenshot(width: int = 1080, height: int = 1920, color: str = "black") -> bytes:
    """Create a synthetic screenshot PNG."""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_elements(*specs: tuple[str, str]) -> list[dict]:
    """Build element dicts from (id_or_text, bounds) tuples.

    Example: _make_elements(("btn_ok", "[100,200][300,400]"), ("Cancel", "[500,600][700,800]"))
    """
    elements = []
    for label, bounds in specs:
        el: dict = {"bounds": bounds}
        if label.startswith("btn_") or label.startswith("tv_"):
            el["id"] = label
        else:
            el["text"] = label
        elements.append(el)
    return elements


def _open_result(png_bytes: bytes) -> Image.Image:
    """Open annotated PNG bytes as a PIL Image."""
    return Image.open(io.BytesIO(png_bytes))


# --- Core behavior ---


class TestAnnotateScreenshot:
    """Core annotation function."""

    def test_returns_valid_png(self) -> None:
        """Annotated output must be a valid PNG."""
        screenshot = _make_screenshot()
        elements = _make_elements(("btn_ok", "[100,200][300,400]"))
        result = annotate_screenshot(screenshot, elements)
        img = _open_result(result)
        assert img.format == "PNG"

    def test_draws_bounding_box(self) -> None:
        """The annotated image must have non-black pixels where the box is drawn."""
        screenshot = _make_screenshot(color="black")
        elements = _make_elements(("btn_ok", "[100,200][300,400]"))
        result = annotate_screenshot(screenshot, elements, max_size=0)  # no scaling
        img = _open_result(result)

        # Sample a pixel on the box edge (x=100, y=200) — should not be black
        px = img.getpixel((100, 200))
        assert px != (0, 0, 0), "Bounding box should be visible on the edge"

    def test_multiple_elements_get_distinct_labels(self) -> None:
        """Each element gets a unique numeric label."""
        screenshot = _make_screenshot()
        elements = _make_elements(
            ("btn_a", "[50,50][200,150]"),
            ("btn_b", "[300,300][500,450]"),
            ("btn_c", "[600,600][800,750]"),
        )
        result = annotate_screenshot(screenshot, elements, max_size=0)
        img = _open_result(result)

        # All three boxes should have drawn something — image should differ from input
        original = _open_result(_make_screenshot(color="black"))
        assert img.tobytes() != original.tobytes(), "Annotation should modify the image"

    def test_empty_elements_returns_unchanged_image(self) -> None:
        """No elements = no annotation, but still valid PNG."""
        screenshot = _make_screenshot(width=200, height=200, color="blue")
        result = annotate_screenshot(screenshot, [], max_size=0)
        img = _open_result(result)
        assert img.size == (200, 200)

    def test_element_without_bounds_skipped(self) -> None:
        """Elements missing bounds should be silently skipped."""
        screenshot = _make_screenshot()
        elements = [{"text": "no bounds"}, {"text": "has bounds", "bounds": "[10,10][100,100]"}]
        result = annotate_screenshot(screenshot, elements, max_size=0)
        # Should not raise — just skip the boundless element
        assert len(result) > 0

    def test_malformed_bounds_skipped(self) -> None:
        """Malformed bounds strings should be silently skipped."""
        screenshot = _make_screenshot()
        elements = [
            {"text": "bad", "bounds": "garbage"},
            {"text": "good", "bounds": "[10,20][30,40]"},
        ]
        # Should not raise
        result = annotate_screenshot(screenshot, elements, max_size=0)
        assert len(result) > 0


# --- Scaling ---


class TestAnnotationScaling:
    """Output scaling to max_size."""

    def test_downscales_to_max_size(self) -> None:
        """Large screenshots should be downscaled so longest edge <= max_size."""
        screenshot = _make_screenshot(width=2160, height=3840)
        elements = _make_elements(("btn_ok", "[100,200][300,400]"))
        result = annotate_screenshot(screenshot, elements, max_size=1280)
        img = _open_result(result)
        assert max(img.size) <= 1280

    def test_preserves_aspect_ratio(self) -> None:
        """Scaling must preserve aspect ratio."""
        screenshot = _make_screenshot(width=1080, height=1920)
        elements = _make_elements(("btn_ok", "[100,200][300,400]"))
        result = annotate_screenshot(screenshot, elements, max_size=540)
        img = _open_result(result)
        # Original is 1080x1920 (9:16). Scaled to max 540 → 304x540 (rounding ok)
        w, h = img.size
        original_ratio = 1080 / 1920
        result_ratio = w / h
        assert abs(original_ratio - result_ratio) < 0.02, f"Aspect ratio drift: {result_ratio}"

    def test_no_upscale_when_smaller_than_max(self) -> None:
        """Images already smaller than max_size should not be upscaled."""
        screenshot = _make_screenshot(width=640, height=480)
        elements = _make_elements(("btn_ok", "[10,10][100,100]"))
        result = annotate_screenshot(screenshot, elements, max_size=1280)
        img = _open_result(result)
        assert img.size == (640, 480)

    def test_max_size_zero_means_no_scaling(self) -> None:
        """max_size=0 disables scaling entirely."""
        screenshot = _make_screenshot(width=2160, height=3840)
        elements = []
        result = annotate_screenshot(screenshot, elements, max_size=0)
        img = _open_result(result)
        assert img.size == (2160, 3840)


# --- Bounds parsing ---


class TestBoundsParsing:
    """Verify bounds string parsing handles edge cases."""

    def test_standard_bounds_format(self) -> None:
        """Standard Android bounds format [x1,y1][x2,y2]."""
        screenshot = _make_screenshot(width=500, height=500, color="black")
        elements = _make_elements(("btn", "[0,0][500,500]"))
        result = annotate_screenshot(screenshot, elements, max_size=0)
        img = _open_result(result)
        # Full-screen box — corners should have color
        assert img.getpixel((0, 0)) != (0, 0, 0)

    def test_zero_area_bounds_skipped(self) -> None:
        """Zero-area bounds (x1==x2 or y1==y2) should be skipped."""
        screenshot = _make_screenshot()
        elements = [{"text": "zero", "bounds": "[100,100][100,200]"}]  # zero width
        result = annotate_screenshot(screenshot, elements, max_size=0)
        assert len(result) > 0  # should not crash


# --- Return type contract ---


class TestReturnContract:
    """annotate_screenshot returns bytes (PNG)."""

    def test_return_type_is_bytes(self) -> None:
        screenshot = _make_screenshot()
        elements = _make_elements(("btn", "[10,10][100,100]"))
        result = annotate_screenshot(screenshot, elements)
        assert isinstance(result, bytes)

    def test_result_is_rgb_not_rgba(self) -> None:
        """Output should be RGB PNG (not RGBA) to match screenshot format."""
        screenshot = _make_screenshot()
        elements = _make_elements(("btn", "[10,10][100,100]"))
        result = annotate_screenshot(screenshot, elements, max_size=0)
        img = _open_result(result)
        assert img.mode == "RGB"


class TestAnnotateScreenshotPil:
    """annotate_screenshot_pil accepts/returns PIL Images (no PNG round-trip)."""

    def test_returns_pil_image(self) -> None:
        img = Image.new("RGB", (1080, 1920), "black")
        elements = _make_elements(("btn", "[10,10][100,100]"))
        result = annotate_screenshot_pil(img, elements)
        assert isinstance(result, Image.Image)

    def test_draws_bounding_boxes(self) -> None:
        """Should draw on the image — pixel at box edge should differ from black."""
        img = Image.new("RGB", (200, 200), "black")
        elements = _make_elements(("btn", "[50,50][150,150]"))
        result = annotate_screenshot_pil(img, elements, max_size=0)
        px = result.getpixel((50, 50))
        assert px != (0, 0, 0), "Box edge should be drawn"

    def test_matches_bytes_version(self) -> None:
        """PIL version should produce same pixels as bytes version."""
        screenshot_bytes = _make_screenshot(width=200, height=200)
        elements = _make_elements(("btn", "[10,10][100,100]"))

        bytes_result = annotate_screenshot(screenshot_bytes, elements, max_size=0)
        bytes_img = Image.open(io.BytesIO(bytes_result))

        pil_input = Image.open(io.BytesIO(screenshot_bytes))
        pil_result = annotate_screenshot_pil(pil_input, elements, max_size=0)

        # Pixel-level comparison (.tobytes() avoids the deprecated .getdata())
        assert bytes_img.size == pil_result.size
        assert bytes_img.tobytes() == pil_result.tobytes()
