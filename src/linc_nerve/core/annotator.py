"""Screenshot annotation — draw bounding boxes and element IDs on screenshots.

Pure function: takes PNG bytes (or PIL Image) + element list, returns annotated result.
No device/MCP dependencies — usable from tools, crucible, BVTs, etc.
"""

from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw, ImageFont

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

# Box drawing style
_BOX_COLOR = (255, 50, 50)  # red
_BOX_WIDTH = 3
_LABEL_BG = (255, 50, 50)  # red background
_LABEL_FG = (255, 255, 255)  # white text


def annotate_screenshot_pil(
    img: Image.Image,
    elements: list[dict],
    max_size: int = 1280,
) -> Image.Image:
    """Draw numbered bounding boxes on a PIL Image.

    Args:
        img: PIL Image (will be converted to RGB if needed).
        elements: Element dicts with "bounds" keys like "[x1,y1][x2,y2]".
        max_size: Downscale so longest edge <= this. 0 = no scaling.

    Returns:
        Annotated PIL Image.
    """
    img = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
    except OSError:
        font = ImageFont.load_default()

    for idx, element in enumerate(elements):
        bounds_str = element.get("bounds", "")
        m = _BOUNDS_RE.match(bounds_str)
        if not m:
            continue

        x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))

        # Skip zero-area boxes
        if x1 >= x2 or y1 >= y2:
            continue

        # Draw bounding box
        draw.rectangle([x1, y1, x2, y2], outline=_BOX_COLOR, width=_BOX_WIDTH)

        # Draw label background + text
        label = str(idx)
        bbox = font.getbbox(label)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = 4
        lx, ly = x1, max(y1 - th - pad * 2, 0)
        draw.rectangle([lx, ly, lx + tw + pad * 2, ly + th + pad * 2], fill=_LABEL_BG)
        draw.text((lx + pad, ly + pad), label, fill=_LABEL_FG, font=font)

    # Scale if needed
    if max_size > 0:
        longest = max(img.size)
        if longest > max_size:
            scale = max_size / longest
            new_w = round(img.width * scale)
            new_h = round(img.height * scale)
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    return img


def annotate_screenshot(
    image_bytes: bytes,
    elements: list[dict],
    max_size: int = 1280,
) -> bytes:
    """Draw numbered bounding boxes on a screenshot.

    Args:
        image_bytes: Raw PNG screenshot bytes.
        elements: Element dicts with "bounds" keys like "[x1,y1][x2,y2]".
        max_size: Downscale so longest edge <= this. 0 = no scaling.

    Returns:
        Annotated PNG as bytes.
    """
    img = Image.open(io.BytesIO(image_bytes))
    result = annotate_screenshot_pil(img, elements, max_size)
    buf = io.BytesIO()
    result.save(buf, format="PNG")
    return buf.getvalue()
