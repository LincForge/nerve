"""Visual UI grounding via Meta SAM 3.1 Promptable Concept Segmentation.

When `nerve_elements` / `nerve_state` return an empty accessibility tree
(e.g. `uiautomator dump` killed on Android 14+, Flutter/Unity/WebView custom
canvases, or unlabelled icon buttons), callers can ground natural-language
visual descriptions ("the green Check Status chip", "the gear settings icon")
directly on the screenshot frame using SAM 3.1 (`POST /v1/responses`) and decode
1-bit masks via `meta-sam-parser`.

Coordinate invariant (LINC rule 99f5b331):
SAM 3.1 boxes and masks are returned in the pixel coordinate space of the image
sent to the model `(W_img, H_img)`, whereas `nerve_tap(x, y)` expects physical
device display coordinates `(W_screen, H_screen)` from `ctrl.get_screen_size()`.
All returned `bounds` and `center` coordinates are scaled by
`(W_screen / W_img, H_screen / H_img)` when `screen_size` is provided.
"""

from __future__ import annotations

import base64
import io
import math
import os
import subprocess
from typing import Any

import httpx
import structlog
from meta_sam_parser import (
    CompletedOutcome,
    SegmentationBoxRecord,
    SegmentationMaskRecord,
    decode_mask_to_raster,
    image_segmentation_format,
)
from PIL import Image, ImageDraw

from linc_nerve.core.action_log import COORDINATE_MAX, parse_bounds

log = structlog.get_logger().bind(domain="grounding")

DEFAULT_META_BASE_URL = "https://api.meta.ai/v1"
SAM_MODEL_ID = "sam-3.1"
DEFAULT_TIMEOUT_S = 15.0


def _keychain_secret(account: str, service: str = "linc-env") -> str | None:
    """Read an API key from macOS Keychain without logging or echoing it."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    secret = (proc.stdout or "").strip()
    return secret or None


def resolve_meta_api_key(api_key: str | None = None) -> str:
    """Resolve `META_AI_API_KEY` from argument, environment, or macOS Keychain."""
    if isinstance(api_key, str) and api_key.strip():
        return api_key.strip()
    for env_name in ("META_AI_API_KEY", "META_API_KEY"):
        val = (os.environ.get(env_name) or "").strip()
        if val:
            return val
    for account in ("META_AI_API_KEY", "META_API_KEY"):
        secret = _keychain_secret(account)
        if secret:
            return secret
    raise ValueError(
        "Missing Meta API key: set META_AI_API_KEY in environment or Keychain (service 'linc-env')"
    )


def _clamp_coord(value: float, upper: int) -> int:
    """Round and clamp a finite coordinate into `0..min(upper, COORDINATE_MAX)`."""
    if not math.isfinite(value):
        return 0
    limit = max(0, min(int(upper), COORDINATE_MAX))
    return max(0, min(limit, round(value)))


def _mask_centroid(mask_rec: SegmentationMaskRecord) -> tuple[float, float] | None:
    """Compute the center of mass of a 1-bit SAM 3.1 mask in source image pixels."""
    try:
        raster = decode_mask_to_raster(mask_rec.mask)
    except Exception as exc:
        log.debug("sam_mask_decode_failed", error=str(exc))
        return None

    width = int(mask_rec.mask.width)
    height = int(mask_rec.mask.height)
    if width <= 0 or height <= 0 or len(raster) < width * height:
        return None

    offset_x = int(mask_rec.bounds.left)
    offset_y = int(mask_rec.bounds.top)
    sum_x = 0
    sum_y = 0
    count = 0
    for idx, pixel in enumerate(raster[: width * height]):
        if pixel:
            sum_x += offset_x + (idx % width)
            sum_y += offset_y + (idx // width)
            count += 1
    if count == 0:
        return None
    return (sum_x / count, sum_y / count)


def _scale_pair(
    img_size: tuple[int, int], screen_size: dict[str, int] | None
) -> tuple[float, float, int, int]:
    """Return `(scale_x, scale_y, max_x, max_y)` from image size to device screen size."""
    img_w = max(1, int(img_size[0]))
    img_h = max(1, int(img_size[1]))
    if isinstance(screen_size, dict):
        sw = int(screen_size.get("width") or 0)
        sh = int(screen_size.get("height") or 0)
        if sw > 0 and sh > 0:
            return sw / img_w, sh / img_h, sw - 1, sh - 1
    return 1.0, 1.0, img_w - 1, img_h - 1


def _extract_response_text(payload: dict[str, Any]) -> str:
    """Concatenate all text segments from a `/v1/responses` JSON payload."""
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
    return "".join(chunks)


def _call_sam31_api(
    png_bytes: bytes,
    prompt: str,
    api_key: str,
    base_url: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Call `POST /v1/responses` with `model="sam-3.1"` and return the segmentation wire string."""
    resolved_base = (
        base_url or os.environ.get("META_API_BASE_URL") or DEFAULT_META_BASE_URL
    ).rstrip("/")
    b64_img = base64.b64encode(png_bytes).decode("ascii")
    body = {
        "model": SAM_MODEL_ID,
        "stream": False,
        "metadata": {"include_confidence": "true", "mask_encoding": "one_bit"},
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": f"data:image/png;base64,{b64_img}"},
                ],
            }
        ],
    }
    resp = httpx.post(
        f"{resolved_base}/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=body,
        timeout=timeout_s,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("SAM 3.1 API returned non-object JSON")
    return _extract_response_text(data)


def _build_target_node(
    object_id: str,
    prompt: str,
    bounds_half_open: tuple[int, int, int, int],
    centroid_img: tuple[float, float],
    confidence: float,
    scale_info: tuple[float, float, int, int],
) -> dict[str, Any]:
    """Scale one grounded object into an `ElementNode`-compatible dict."""
    scale_x, scale_y, max_x, max_y = scale_info
    left, top, right, bottom = bounds_half_open
    x2_inc = max(left, right - 1)
    y2_inc = max(top, bottom - 1)
    sx1 = _clamp_coord(left * scale_x, max_x)
    sy1 = _clamp_coord(top * scale_y, max_y)
    sx2 = max(sx1, _clamp_coord(x2_inc * scale_x, max_x))
    sy2 = max(sy1, _clamp_coord(y2_inc * scale_y, max_y))
    scx = _clamp_coord(centroid_img[0] * scale_x, max_x)
    scy = _clamp_coord(centroid_img[1] * scale_y, max_y)
    safe_conf = confidence if (math.isfinite(confidence) and 0.0 <= confidence <= 1.0) else 0.0
    return {
        "text": prompt,
        "desc": prompt,
        "class": "VisualTarget",
        "bounds": f"[{sx1},{sy1}][{sx2},{sy2}]",
        "center": {"x": scx, "y": scy},
        "confidence": round(safe_conf, 4),
        "object_id": object_id,
        "clickable": True,
        "source": SAM_MODEL_ID,
    }


def parse_sam31_records(
    raw_text: str,
    prompt: str,
    img_size: tuple[int, int],
    screen_size: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Parse SAM 3.1 wire text into `ElementNode`-compatible dicts sorted by confidence."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        return []

    parser = image_segmentation_format().create_parser()
    parser.push(raw_text.strip())
    finish = parser.finish(CompletedOutcome())
    if finish.result is None:
        return []

    boxes: dict[str, SegmentationBoxRecord] = {}
    masks: dict[str, SegmentationMaskRecord] = {}
    order: list[str] = []
    for rec in finish.result.records:
        if isinstance(rec, SegmentationBoxRecord):
            boxes[rec.object_id] = rec
            if rec.object_id not in order:
                order.append(rec.object_id)
        elif isinstance(rec, SegmentationMaskRecord):
            masks[rec.object_id] = rec
            if rec.object_id not in order:
                order.append(rec.object_id)

    scale_info = _scale_pair(img_size, screen_size)
    targets: list[dict[str, Any]] = []
    for obj_id in order:
        box_rec = boxes.get(obj_id)
        mask_rec = masks.get(obj_id)
        if box_rec is not None:
            half_open = (box_rec.left, box_rec.top, box_rec.right, box_rec.bottom)
        elif mask_rec is not None:
            b = mask_rec.bounds
            half_open = (b.left, b.top, b.right, b.bottom)
        else:
            continue

        centroid = _mask_centroid(mask_rec) if mask_rec is not None else None
        if centroid is None:
            left, top, right, bottom = half_open
            centroid = ((left + max(left, right - 1)) / 2.0, (top + max(top, bottom - 1)) / 2.0)

        if box_rec is not None and box_rec.confidence is not None:
            conf = box_rec.confidence
        elif mask_rec is not None and mask_rec.confidence is not None:
            conf = mask_rec.confidence
        else:
            conf = 0.0
        targets.append(
            _build_target_node(obj_id, prompt, half_open, centroid, float(conf), scale_info)
        )

    targets.sort(key=lambda item: float(item.get("confidence", 0.0)), reverse=True)
    return targets


def ground_visual_targets(
    image_bytes: bytes,
    prompt: str,
    screen_size: dict[str, int] | None = None,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Ground *prompt* on *image_bytes* using SAM 3.1 and return scaled target nodes."""
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        raise ValueError("image_bytes must be non-empty bytes")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    clean_prompt = prompt.strip()
    resolved_key = resolve_meta_api_key(api_key)

    with Image.open(io.BytesIO(image_bytes)) as img:
        img_w, img_h = img.size
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        png_bytes = buf.getvalue()

    raw_text = _call_sam31_api(png_bytes, clean_prompt, resolved_key)
    return parse_sam31_records(raw_text, clean_prompt, (img_w, img_h), screen_size=screen_size)


def render_grounded_overlay(
    image_bytes: bytes,
    targets: list[dict[str, Any]],
    screen_size: dict[str, int] | None = None,
) -> bytes:
    """Render translucent green bounding boxes and red centroid crosshairs onto *image_bytes*."""
    with Image.open(io.BytesIO(image_bytes)) as src:
        base = src.convert("RGBA")
    img_w, img_h = base.size
    scale_x, scale_y, _, _ = _scale_pair((img_w, img_h), screen_size)
    inv_x = 1.0 / scale_x if scale_x > 0 else 1.0
    inv_y = 1.0 / scale_y if scale_y > 0 else 1.0

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for idx, target in enumerate(targets):
        rect = parse_bounds(str(target.get("bounds") or ""))
        if rect is not None:
            x1, y1, x2, y2 = rect
            ix1, iy1 = round(x1 * inv_x), round(y1 * inv_y)
            ix2, iy2 = max(ix1 + 1, round(x2 * inv_x)), max(iy1 + 1, round(y2 * inv_y))
            draw.rectangle(
                [ix1, iy1, ix2, iy2],
                fill=(0, 220, 90, 56),
                outline=(0, 220, 90, 235),
                width=2,
            )
            conf = target.get("confidence", 0.0)
            label = f"#{idx} {conf:.2f}" if isinstance(conf, (int, float)) else f"#{idx}"
            label_box = [ix1, max(0, iy1 - 14), ix1 + len(label) * 7 + 6, iy1]
            draw.rectangle(label_box, fill=(0, 160, 65, 230))
            draw.text((ix1 + 3, max(0, iy1 - 13)), label, fill=(255, 255, 255, 255))

        center = target.get("center")
        if isinstance(center, dict) and "x" in center and "y" in center:
            cx = round(float(center["x"]) * inv_x)
            cy = round(float(center["y"]) * inv_y)
            r = 5
            draw.ellipse(
                [cx - r, cy - r, cx + r, cy + r],
                fill=(255, 50, 50, 240),
                outline=(255, 255, 255, 255),
            )

    composited = Image.alpha_composite(base, overlay).convert("RGB")
    out = io.BytesIO()
    composited.save(out, format="PNG")
    return out.getvalue()


def should_suggest_visual_grounding(
    elements: list[dict[str, Any]] | None,
    elements_error: str | None = None,
) -> bool:
    """Return True when the UI tree is empty, errored, or has unlabelled clickable nodes."""
    if elements_error or not elements:
        return True
    clickable = [el for el in elements if isinstance(el, dict) and el.get("clickable")]
    if not clickable:
        return False
    unlabelled = [
        el
        for el in clickable
        if not (el.get("text") or el.get("desc") or el.get("id") or el.get("resource_id"))
    ]
    return len(unlabelled) > 0


VISUAL_GROUNDING_HINT = (
    "UI hierarchy is empty or contains unlabelled clickable controls on this screen. "
    "Use nerve_ground(device, prompt=..., annotate=True) or nerve_tap(device, prompt=...) "
    "for Meta SAM 3.1 visual concept grounding."
)
