"""State engine for delta detection and smart diffing.

Tracks per-device state (screenshot hashes, element snapshots, foreground app,
orientation) to minimize redundant data sent to AI consumers.

Phase 2 deliverable — reduces token consumption ~10x for static screens.
"""

from __future__ import annotations

import hashlib
import io
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from PIL import Image

from .contracts import require

logger = structlog.get_logger().bind(domain="device_control")

# --- Perceptual Hash ---

_HASH_SIZE = 8  # 8x8 = 64 bits


def _average_hash(image_bytes: bytes) -> int:
    """Compute a 64-bit average perceptual hash from PNG bytes.

    Downscales to 8x8 grayscale, compares each pixel to the mean.
    Returns an integer where each bit represents above/below average.
    For solid-color images, falls back to a content hash of raw bytes.
    """
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("L").resize((_HASH_SIZE, _HASH_SIZE), Image.Resampling.LANCZOS)
    pixels = list(img.tobytes())
    avg = sum(pixels) / len(pixels)

    # Solid-color images have zero variance — use content hash instead
    if all(p == pixels[0] for p in pixels):
        return int.from_bytes(hashlib.md5(image_bytes, usedforsecurity=False).digest()[:8])

    bits = 0
    for px in pixels:
        bits = (bits << 1) | (1 if px >= avg else 0)
    return bits


def _average_hash_pil(img: Image.Image) -> int:
    """Compute a 64-bit average perceptual hash from a PIL Image (no PNG decode).

    Same algorithm as _average_hash but accepts a PIL Image directly,
    skipping the PNG bytes → Image.open decode step.
    """
    gray = img.convert("L").resize((_HASH_SIZE, _HASH_SIZE), Image.Resampling.LANCZOS)
    pixels = list(gray.tobytes())
    avg = sum(pixels) / len(pixels)

    if all(p == pixels[0] for p in pixels):
        return int.from_bytes(hashlib.md5(gray.tobytes(), usedforsecurity=False).digest()[:8])

    bits = 0
    for px in pixels:
        bits = (bits << 1) | (1 if px >= avg else 0)
    return bits


def _hamming_distance(a: int, b: int) -> int:
    """Count differing bits between two hashes."""
    return bin(a ^ b).count("1")


# --- Content Hash (for exact element comparison) ---


def _content_hash(data: Any) -> str:
    """Fast hash of serialized data for exact-match comparison."""
    import json

    raw = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()[:12]


# --- Element Diff ---


def _element_key(elem: dict) -> str:
    """Identity key for an element — resource_id + bounds, or text + bounds."""
    rid = elem.get("id", "")
    bounds = elem.get("bounds", "")
    text = elem.get("text", "")
    if rid:
        return f"{rid}@{bounds}"
    return f"${text}@{bounds}"


def diff_elements(
    old: list[dict],
    new: list[dict],
) -> dict[str, Any]:
    """Compute a diff between two element snapshots.

    Returns:
        {"added": [...], "removed": [...], "changed": [...], "unchanged_count": N}
    """
    old_map = {_element_key(e): e for e in old}
    new_map = {_element_key(e): e for e in new}

    old_keys = set(old_map.keys())
    new_keys = set(new_map.keys())

    added = [new_map[k] for k in sorted(new_keys - old_keys)]
    removed = [old_map[k] for k in sorted(old_keys - new_keys)]

    changed: list[dict] = []
    unchanged = 0
    for k in sorted(old_keys & new_keys):
        if old_map[k] != new_map[k]:
            changed.append(new_map[k])
        else:
            unchanged += 1

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": unchanged,
    }


# --- Device State Session ---

# Default threshold: 5 bits out of 64 = ~8% visual difference
SCREENSHOT_CHANGE_THRESHOLD = 5

# How long after a recorded crash a foreground change is attributed to it.
# A crash usually drops the app to launcher/home within a second or two; 10s
# tolerates slow ANR teardown without falsely blaming unrelated navigation.
_CRASH_ATTRIBUTION_WINDOW = 10.0


@dataclass
class DeviceStateSession:
    """Tracks state for a single device across tool calls."""

    serial: str
    last_screenshot_hash: int | None = None
    last_screenshot_time: float = 0.0
    last_elements: list[dict] = field(default_factory=list)
    last_elements_hash: str = ""
    # Use ``None`` sentinels for foreground/orientation so the FIRST observation
    # establishes baseline silently rather than reporting a phantom change.
    # See bug 2b2dbef6.
    last_foreground: dict[str, str] | None = None
    last_orientation: str | None = None
    frames_since_change: int = 0
    total_screenshots: int = 0
    total_unchanged: int = 0
    # Crash attribution: package -> monotonic time of last observed crash, and a
    # sticky flag set by the most recent update_foreground() call so callers can
    # read it without a branch of their own (keeps _collect_*_state CC flat).
    recent_crashes: dict[str, float] = field(default_factory=dict)
    foreground_changed_due_to_crash: bool = False

    def check_screenshot(
        self,
        image_bytes: bytes,
        threshold: int = SCREENSHOT_CHANGE_THRESHOLD,
    ) -> tuple[bool, int]:
        """Check if a screenshot differs from the last one.

        First call establishes the baseline silently — there is no prior frame
        to compute a difference against, so reporting ``changed=True`` with
        ``hamming=0`` is a lie. See bug 2b2dbef6.

        Returns:
            (changed: bool, hamming_distance: int)
        """
        new_hash = _average_hash(image_bytes)
        self.total_screenshots += 1

        if self.last_screenshot_hash is None:
            self.last_screenshot_hash = new_hash
            self.last_screenshot_time = time.monotonic()
            self.frames_since_change = 0
            return False, 0

        distance = _hamming_distance(self.last_screenshot_hash, new_hash)
        changed = distance > threshold

        if changed:
            self.last_screenshot_hash = new_hash
            self.last_screenshot_time = time.monotonic()
            self.frames_since_change = 0
        else:
            self.frames_since_change += 1
            self.total_unchanged += 1

        return changed, distance

    def update_elements(self, elements: list[dict]) -> dict[str, Any] | None:
        """Update element snapshot and return diff if changed.

        Returns None if elements unchanged, diff dict otherwise.
        """
        new_hash = _content_hash(elements)
        if new_hash == self.last_elements_hash:
            return None

        diff = diff_elements(self.last_elements, elements) if self.last_elements else None
        self.last_elements = elements
        self.last_elements_hash = new_hash
        return diff

    def record_crash(self, package: str) -> None:
        """Note that *package* crashed now (for foreground-change attribution)."""
        self.recent_crashes[package] = time.monotonic()

    def _crash_attributed(self, prev_foreground: dict[str, str], changed: bool) -> bool:
        """Whether a foreground change away from *prev_foreground* was a crash.

        True only when the app we just left crashed within the attribution
        window. Factored out so update_foreground stays branch-flat.
        """
        if not changed:
            return False
        crashed_at = self.recent_crashes.get(prev_foreground.get("package", ""))
        return crashed_at is not None and (time.monotonic() - crashed_at) <= (
            _CRASH_ATTRIBUTION_WINDOW
        )

    def update_foreground(self, foreground: dict[str, str]) -> bool:
        """Update foreground app and return whether it changed.

        Also sets ``foreground_changed_due_to_crash`` when the app we just left
        crashed within the attribution window (read it after this call). First
        call establishes baseline silently (returns False). See bug 2b2dbef6.
        """
        prev = self.last_foreground
        if prev is None:
            self.foreground_changed_due_to_crash = False
            self.last_foreground = foreground
            return False
        changed = foreground != prev
        self.foreground_changed_due_to_crash = self._crash_attributed(prev, changed)
        self.last_foreground = foreground
        return changed

    def update_orientation(self, orientation: str) -> bool:
        """Update orientation and return whether it changed.

        First call establishes baseline silently (returns False). See bug 2b2dbef6.
        """
        if self.last_orientation is None:
            self.last_orientation = orientation
            return False
        changed = orientation != self.last_orientation
        self.last_orientation = orientation
        return changed

    def stats(self) -> dict[str, Any]:
        """Return session statistics."""
        return {
            "serial": self.serial,
            "total_screenshots": self.total_screenshots,
            "total_unchanged": self.total_unchanged,
            "savings_pct": (
                round(self.total_unchanged / self.total_screenshots * 100, 1)
                if self.total_screenshots > 0
                else 0
            ),
            "frames_since_change": self.frames_since_change,
        }


# --- Session Manager ---


class StateSessionManager:
    """Manages state sessions for all connected devices.

    Thread-safe: FastMCP dispatches tool handlers from a thread pool,
    so concurrent calls may access ``_sessions`` simultaneously.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, DeviceStateSession] = {}
        self._lock = threading.Lock()

    def get(self, serial: str) -> DeviceStateSession:
        """Get or create a state session for a device."""
        with self._lock:
            if serial not in self._sessions:
                self._sessions[serial] = DeviceStateSession(serial=serial)
            return self._sessions[serial]

    def remove(self, serial: str) -> None:
        """Remove a device session."""
        with self._lock:
            self._sessions.pop(serial, None)

    def clear(self) -> None:
        """Clear all sessions."""
        with self._lock:
            self._sessions.clear()

    @property
    def active_sessions(self) -> dict[str, dict[str, Any]]:
        """Return stats for all active sessions.

        Snapshot the dict under lock, then compute stats outside to avoid
        holding the lock while session.stats() runs (which could block
        concurrent get/create calls from other MCP threads).
        """
        with self._lock:
            sessions_snapshot = list(self._sessions.items())
        return {serial: session.stats() for serial, session in sessions_snapshot}


# --- UI Stability Polling ---


def wait_for_stable_ui(
    screenshot_fn: Any,
    stable_ms: int = 300,
    timeout_ms: int = 3000,
    poll_ms: int = 50,
    threshold: int = SCREENSHOT_CHANGE_THRESHOLD,
    pil_mode: bool = False,
) -> dict[str, Any]:
    """Poll screenshots until the UI is visually stable.

    Captures screenshots at ``poll_ms`` intervals, comparing perceptual hashes.
    Returns when the screen has been unchanged for ``stable_ms`` consecutively,
    or when ``timeout_ms`` is exceeded.

    Args:
        screenshot_fn: Callable returning PNG bytes or PIL Image (if pil_mode=True).
        stable_ms: Required consecutive stable duration in ms (default 300).
        timeout_ms: Hard timeout in ms (default 3000).
        poll_ms: Polling interval in ms (default 50).
        threshold: Hamming distance threshold for "changed" (default 5).
        pil_mode: If True, screenshot_fn returns PIL Image and hashing skips PNG decode.
            Result includes "last_frame_pil" key with PIL Image.

    Returns:
        {"stable": bool, "elapsed_ms": float, "polls": int, "last_frame": bytes|None,
         "last_frame_pil": Image|None}
    """
    hash_fn = _average_hash_pil if pil_mode else _average_hash
    t0 = time.monotonic()
    deadline = t0 + timeout_ms / 1000.0
    polls = 0
    last_hash: int | None = None
    stable_since: float | None = None
    last_data: Any = None  # bytes or PIL Image depending on pil_mode

    while True:
        data = screenshot_fn()
        last_data = data
        polls += 1
        current_hash = hash_fn(data)

        if last_hash is None:
            # First frame — start the stability clock
            last_hash = current_hash
            stable_since = time.monotonic()
        else:
            distance = _hamming_distance(last_hash, current_hash)
            if distance > threshold:
                # Screen changed — reset stability clock
                last_hash = current_hash
                stable_since = time.monotonic()
            # else: still stable, stable_since unchanged

        now = time.monotonic()
        if now - require(stable_since, "UI stability start time") >= stable_ms / 1000.0:
            return {
                "stable": True,
                "elapsed_ms": round((now - t0) * 1000, 1),
                "polls": polls,
                "last_frame": None if pil_mode else last_data,
                "last_frame_pil": last_data if pil_mode else None,
            }

        if now >= deadline:
            return {
                "stable": False,
                "elapsed_ms": round((now - t0) * 1000, 1),
                "polls": polls,
                "last_frame": None if pil_mode else last_data,
                "last_frame_pil": last_data if pil_mode else None,
            }

        time.sleep(poll_ms / 1000.0)
