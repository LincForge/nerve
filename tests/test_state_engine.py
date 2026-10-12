"""Tests for state engine — delta detection and smart diffing."""

from __future__ import annotations

import io

from PIL import Image

from linc_nerve.core.state_engine import (
    DeviceStateSession,
    StateSessionManager,
    _average_hash,
    _average_hash_pil,
    _hamming_distance,
    diff_elements,
    wait_for_stable_ui,
)


def _make_png(color: tuple[int, int, int] = (128, 128, 128), size: int = 64) -> bytes:
    """Create a solid-color PNG for testing."""
    img = Image.new("RGB", (size, size), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_gradient_png(offset: int = 0, size: int = 64) -> bytes:
    """Create a gradient PNG — different offsets produce different hashes."""
    img = Image.new("L", (size, size))
    pixels = img.load()
    assert pixels is not None
    for y in range(size):
        for x in range(size):
            pixels[x, y] = (x * 4 + y * 4 + offset) % 256
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --- Perceptual Hash ---


class TestAverageHash:
    def test_identical_images_same_hash(self) -> None:
        png = _make_png((100, 100, 100))
        assert _average_hash(png) == _average_hash(png)

    def test_different_colors_different_hash(self) -> None:
        white = _make_png((255, 255, 255))
        black = _make_png((0, 0, 0))
        # Solid images hash to all-1s or all-0s — max distance
        h1 = _average_hash(white)
        h2 = _average_hash(black)
        assert h1 != h2

    def test_similar_images_low_distance(self) -> None:
        a = _make_gradient_png(offset=0)
        b = _make_gradient_png(offset=1)
        h1 = _average_hash(a)
        h2 = _average_hash(b)
        dist = _hamming_distance(h1, h2)
        assert dist <= 10  # Very similar

    def test_different_images_high_distance(self) -> None:
        a = _make_gradient_png(offset=0)
        b = _make_gradient_png(offset=128)
        h1 = _average_hash(a)
        h2 = _average_hash(b)
        dist = _hamming_distance(h1, h2)
        assert dist > 5  # Meaningfully different


class TestAverageHashPil:
    """Tests for PIL-native perceptual hash (no PNG decode)."""

    def test_matches_bytes_version_gradient(self) -> None:
        """PIL hash should match bytes hash for the same gradient image."""
        png = _make_gradient_png(offset=42)
        img = Image.open(io.BytesIO(png))
        assert _average_hash_pil(img) == _average_hash(png)

    def test_different_images_different_hash(self) -> None:
        img_a = Image.open(io.BytesIO(_make_gradient_png(offset=0)))
        img_b = Image.open(io.BytesIO(_make_gradient_png(offset=128)))
        h1 = _average_hash_pil(img_a)
        h2 = _average_hash_pil(img_b)
        assert _hamming_distance(h1, h2) > 5

    def test_solid_color_returns_nonzero(self) -> None:
        """Solid-color images should still produce a usable hash."""
        img = Image.new("RGB", (64, 64), (100, 100, 100))
        h = _average_hash_pil(img)
        assert isinstance(h, int)
        assert h != 0


class TestHammingDistance:
    def test_identical(self) -> None:
        assert _hamming_distance(0b1010, 0b1010) == 0

    def test_one_bit(self) -> None:
        assert _hamming_distance(0b1010, 0b1011) == 1

    def test_all_different(self) -> None:
        assert _hamming_distance(0xFF, 0x00) == 8


# --- Element Diff ---


class TestElementDiff:
    def test_no_change(self) -> None:
        elems = [{"id": "btn", "text": "OK", "bounds": "[0,0][100,50]"}]
        diff = diff_elements(elems, elems)
        assert diff["added"] == []
        assert diff["removed"] == []
        assert diff["changed"] == []
        assert diff["unchanged_count"] == 1

    def test_added_element(self) -> None:
        old = [{"id": "btn1", "text": "OK", "bounds": "[0,0][100,50]"}]
        new = [
            {"id": "btn1", "text": "OK", "bounds": "[0,0][100,50]"},
            {"id": "btn2", "text": "Cancel", "bounds": "[0,60][100,110]"},
        ]
        diff = diff_elements(old, new)
        assert len(diff["added"]) == 1
        assert diff["added"][0]["id"] == "btn2"
        assert diff["removed"] == []

    def test_removed_element(self) -> None:
        old = [
            {"id": "btn1", "text": "OK", "bounds": "[0,0][100,50]"},
            {"id": "btn2", "text": "Cancel", "bounds": "[0,60][100,110]"},
        ]
        new = [{"id": "btn1", "text": "OK", "bounds": "[0,0][100,50]"}]
        diff = diff_elements(old, new)
        assert diff["added"] == []
        assert len(diff["removed"]) == 1
        assert diff["removed"][0]["id"] == "btn2"

    def test_changed_element(self) -> None:
        old = [{"id": "btn1", "text": "OK", "bounds": "[0,0][100,50]"}]
        new = [{"id": "btn1", "text": "Done", "bounds": "[0,0][100,50]"}]
        diff = diff_elements(old, new)
        assert len(diff["changed"]) == 1
        assert diff["changed"][0]["text"] == "Done"

    def test_empty_to_elements(self) -> None:
        diff = diff_elements([], [{"id": "btn", "bounds": "[0,0][100,50]"}])
        assert len(diff["added"]) == 1
        assert diff["removed"] == []

    def test_elements_to_empty(self) -> None:
        diff = diff_elements([{"id": "btn", "bounds": "[0,0][100,50]"}], [])
        assert diff["added"] == []
        assert len(diff["removed"]) == 1

    def test_key_by_text_when_no_id(self) -> None:
        """Elements without resource_id use text+bounds as key."""
        old = [{"text": "Hello", "bounds": "[0,0][100,50]"}]
        new = [{"text": "Hello", "bounds": "[0,0][100,50]"}]
        diff = diff_elements(old, new)
        assert diff["unchanged_count"] == 1


# --- Device State Session ---


class TestDeviceStateSession:
    def test_first_screenshot_establishes_baseline_silently(self) -> None:
        """No prior frame ⇒ no diff to report. Returns (False, 0).

        Bug 2b2dbef6: previously returned (True, 0), which read as
        "screenshot changed but hamming=0" — a contradictory lie.
        """
        session = DeviceStateSession(serial="DEV1")
        png = _make_png()
        changed, dist = session.check_screenshot(png)
        assert changed is False
        assert dist == 0
        assert session.total_screenshots == 1
        # Baseline now set — verify by a second identical frame staying False
        changed2, _ = session.check_screenshot(png)
        assert changed2 is False

    def test_identical_screenshot_unchanged(self) -> None:
        session = DeviceStateSession(serial="DEV1")
        png = _make_png()
        session.check_screenshot(png)

        changed, dist = session.check_screenshot(png)
        assert changed is False
        assert dist == 0
        assert session.frames_since_change == 1
        assert session.total_unchanged == 1

    def test_different_screenshot_detected(self) -> None:
        session = DeviceStateSession(serial="DEV1")
        session.check_screenshot(_make_png((0, 0, 0)))

        changed, dist = session.check_screenshot(_make_png((255, 255, 255)))
        assert changed is True
        assert dist > 0
        assert session.frames_since_change == 0

    def test_frames_since_change_increments(self) -> None:
        session = DeviceStateSession(serial="DEV1")
        png = _make_png()
        session.check_screenshot(png)
        session.check_screenshot(png)
        session.check_screenshot(png)
        assert session.frames_since_change == 2

    def test_update_elements_first_call(self) -> None:
        session = DeviceStateSession(serial="DEV1")
        elems = [{"id": "btn", "text": "OK"}]
        diff = session.update_elements(elems)
        # First call: no prior data, returns None (no meaningful diff)
        assert diff is None
        assert session.last_elements == elems

    def test_update_elements_unchanged(self) -> None:
        session = DeviceStateSession(serial="DEV1")
        elems = [{"id": "btn", "text": "OK"}]
        session.update_elements(elems)

        diff = session.update_elements(elems)
        assert diff is None  # No change

    def test_update_elements_changed(self) -> None:
        session = DeviceStateSession(serial="DEV1")
        session.update_elements([{"id": "btn", "text": "OK", "bounds": "[0,0][100,50]"}])

        diff = session.update_elements([{"id": "btn", "text": "Done", "bounds": "[0,0][100,50]"}])
        assert diff is not None
        assert len(diff["changed"]) == 1

    def test_update_foreground_tracks_change(self) -> None:
        """First call establishes baseline silently. Bug 2b2dbef6."""
        session = DeviceStateSession(serial="DEV1")
        # First call — no prior, returns False (baseline only)
        assert session.update_foreground({"package": "com.app"}) is False
        # Same — no change
        assert session.update_foreground({"package": "com.app"}) is False
        # Different — changed
        assert session.update_foreground({"package": "com.other"}) is True

    def test_update_foreground_first_empty_does_not_flip(self) -> None:
        """An empty foreground on first observation must not register as change.

        Reproduces bug 2b2dbef6: previously empty dict {"package":"","activity":""}
        compared against default {} returned True, lying about a change.
        """
        session = DeviceStateSession(serial="DEV1")
        empty = {"package": "", "activity": ""}
        assert session.update_foreground(empty) is False
        # Identical empty value on subsequent call: still no change
        assert session.update_foreground(empty) is False

    def test_update_orientation_tracks_change(self) -> None:
        """First call establishes baseline silently. Bug 2b2dbef6."""
        session = DeviceStateSession(serial="DEV1")
        assert session.update_orientation("portrait") is False
        assert session.update_orientation("portrait") is False
        assert session.update_orientation("landscape") is True

    def test_foreground_change_attributed_to_crash(self) -> None:
        """A foreground change away from a just-crashed app is flagged (T11)."""
        session = DeviceStateSession(serial="DEV1")
        assert session.update_foreground({"package": "com.app"}) is False  # baseline
        session.record_crash("com.app")
        # App falls to launcher after the crash → change attributed to it.
        assert session.update_foreground({"package": "com.launcher"}) is True
        assert session.foreground_changed_due_to_crash is True

    def test_normal_navigation_not_attributed_to_crash(self) -> None:
        session = DeviceStateSession(serial="DEV1")
        session.update_foreground({"package": "com.app"})  # baseline
        # No crash recorded — a normal navigation must not be flagged.
        assert session.update_foreground({"package": "com.other"}) is True
        assert session.foreground_changed_due_to_crash is False

    def test_crash_outside_window_not_attributed(self) -> None:
        from linc_nerve.core import state_engine

        session = DeviceStateSession(serial="DEV1")
        session.update_foreground({"package": "com.app"})  # baseline
        session.record_crash("com.app")
        # Backdate the crash beyond the attribution window.
        session.recent_crashes["com.app"] -= state_engine._CRASH_ATTRIBUTION_WINDOW + 1
        assert session.update_foreground({"package": "com.launcher"}) is True
        assert session.foreground_changed_due_to_crash is False

    def test_unchanged_foreground_clears_crash_flag(self) -> None:
        session = DeviceStateSession(serial="DEV1")
        session.update_foreground({"package": "com.app"})  # baseline
        session.record_crash("com.app")
        # Same foreground (no change) → not a crash-attributed change.
        assert session.update_foreground({"package": "com.app"}) is False
        assert session.foreground_changed_due_to_crash is False

    def test_stats(self) -> None:
        session = DeviceStateSession(serial="DEV1")
        png = _make_png()
        session.check_screenshot(png)
        session.check_screenshot(png)
        session.check_screenshot(png)

        stats = session.stats()
        assert stats["serial"] == "DEV1"
        assert stats["total_screenshots"] == 3
        assert stats["total_unchanged"] == 2
        assert stats["savings_pct"] == 66.7
        assert stats["frames_since_change"] == 2

    def test_stats_no_screenshots(self) -> None:
        session = DeviceStateSession(serial="DEV1")
        stats = session.stats()
        assert stats["savings_pct"] == 0


# --- Session Manager ---


class TestStateSessionManager:
    def test_get_creates_session(self) -> None:
        mgr = StateSessionManager()
        session = mgr.get("DEV1")
        assert session.serial == "DEV1"

    def test_get_returns_same_session(self) -> None:
        mgr = StateSessionManager()
        s1 = mgr.get("DEV1")
        s2 = mgr.get("DEV1")
        assert s1 is s2

    def test_different_devices_different_sessions(self) -> None:
        mgr = StateSessionManager()
        s1 = mgr.get("DEV1")
        s2 = mgr.get("DEV2")
        assert s1 is not s2

    def test_remove_session(self) -> None:
        mgr = StateSessionManager()
        mgr.get("DEV1")
        mgr.remove("DEV1")
        # Next get creates a fresh session
        s = mgr.get("DEV1")
        assert s.total_screenshots == 0

    def test_clear_all(self) -> None:
        mgr = StateSessionManager()
        mgr.get("DEV1")
        mgr.get("DEV2")
        mgr.clear()
        assert mgr.active_sessions == {}

    def test_active_sessions(self) -> None:
        mgr = StateSessionManager()
        s = mgr.get("DEV1")
        s.check_screenshot(_make_png())

        sessions = mgr.active_sessions
        assert "DEV1" in sessions
        assert sessions["DEV1"]["total_screenshots"] == 1

    def test_concurrent_access(self) -> None:
        """StateSessionManager must be thread-safe (FastMCP thread pool)."""
        import threading

        mgr = StateSessionManager()
        errors: list[Exception] = []

        def worker(device_id: str) -> None:
            try:
                for _ in range(50):
                    s = mgr.get(device_id)
                    s.check_screenshot(_make_png())
                    _ = mgr.active_sessions
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(f"DEV{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread safety violation: {errors}"
        assert len(mgr.active_sessions) == 8

    def test_active_sessions_does_not_block_get(self) -> None:
        """active_sessions must not hold _lock while computing stats.

        Regression test: active_sessions held _lock during session.stats()
        iteration, blocking concurrent get() calls from MCP threads.
        """
        import threading
        import time

        mgr = StateSessionManager()
        # Pre-populate sessions with screenshots so stats() has work to do
        for i in range(5):
            s = mgr.get(f"DEV{i}")
            s.check_screenshot(_make_png())

        stats_running = threading.Event()
        get_completed = threading.Event()

        original_stats = DeviceStateSession.stats
        call_count = 0

        def slow_stats(self: DeviceStateSession) -> dict:
            """Simulate slow stats to widen the contention window."""
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                stats_running.set()  # Signal that iteration started
                time.sleep(0.3)  # Hold long enough for get() to attempt
            return original_stats(self)

        DeviceStateSession.stats = slow_stats  # type: ignore[assignment]
        try:

            def call_active_sessions() -> None:
                _ = mgr.active_sessions

            def call_get() -> None:
                stats_running.wait(timeout=2)  # Wait until stats is running
                mgr.get("NEW_DEVICE")
                get_completed.set()

            t1 = threading.Thread(target=call_active_sessions)
            t2 = threading.Thread(target=call_get)
            t1.start()
            t2.start()

            # get() must complete quickly even while active_sessions iterates
            assert get_completed.wait(timeout=2), (
                "get() blocked by active_sessions — _lock held too long"
            )
            t1.join(timeout=3)
            t2.join(timeout=3)
        finally:
            DeviceStateSession.stats = original_stats  # type: ignore[assignment]


# --- wait_for_stable_ui ---


class TestWaitForStableUI:
    """Tests for screenshot-based UI stability polling."""

    def test_already_stable_returns_immediately(self) -> None:
        """If the screen doesn't change, should return stable quickly."""
        stable_png = _make_png((100, 100, 100))
        call_count = 0

        def screenshot_fn() -> bytes:
            nonlocal call_count
            call_count += 1
            return stable_png

        result = wait_for_stable_ui(screenshot_fn, stable_ms=100, timeout_ms=2000, poll_ms=20)
        assert result["stable"] is True
        assert result["elapsed_ms"] >= 100  # Must wait at least stable_ms
        assert result["polls"] >= 2  # At least 2 polls to confirm stability

    def test_transition_then_stable(self) -> None:
        """Screen changes for a few frames then settles — should detect stability."""
        frames = [
            _make_gradient_png(offset=0),
            _make_gradient_png(offset=50),
            _make_gradient_png(offset=100),  # settles here
        ]
        call_count = 0

        def screenshot_fn() -> bytes:
            nonlocal call_count
            call_count += 1
            if call_count <= len(frames):
                return frames[call_count - 1]
            return frames[-1]  # stable after transition

        result = wait_for_stable_ui(screenshot_fn, stable_ms=100, timeout_ms=3000, poll_ms=20)
        assert result["stable"] is True

    def test_timeout_on_constant_change(self) -> None:
        """If the screen never settles, should timeout."""
        call_count = 0

        def screenshot_fn() -> bytes:
            nonlocal call_count
            call_count += 1
            # Every frame is different
            return _make_gradient_png(offset=call_count * 30)

        result = wait_for_stable_ui(screenshot_fn, stable_ms=200, timeout_ms=500, poll_ms=20)
        assert result["stable"] is False
        assert result["elapsed_ms"] >= 500
        assert result["elapsed_ms"] < 1000  # Shouldn't overshoot too much

    def test_returns_polls_count(self) -> None:
        """Result should include the number of screenshot polls taken."""
        stable_png = _make_png()

        result = wait_for_stable_ui(
            lambda: stable_png,
            stable_ms=50,
            timeout_ms=2000,
            poll_ms=10,
        )
        assert "polls" in result
        assert result["polls"] >= 2

    def test_returns_elapsed_ms(self) -> None:
        """Result should include total elapsed time."""
        stable_png = _make_png()

        result = wait_for_stable_ui(
            lambda: stable_png,
            stable_ms=80,
            timeout_ms=2000,
            poll_ms=20,
        )
        assert "elapsed_ms" in result
        assert isinstance(result["elapsed_ms"], (int, float))

    def test_screenshot_error_propagates(self) -> None:
        """If screenshot_fn raises, wait_for_stable_ui should propagate."""
        import pytest

        def bad_screenshot() -> bytes:
            raise RuntimeError("device disconnected")

        with pytest.raises(RuntimeError, match="device disconnected"):
            wait_for_stable_ui(bad_screenshot)

    def test_default_params(self) -> None:
        """Should work with default parameters (50ms poll, 300ms stable, 3s timeout)."""
        stable_png = _make_png()
        result = wait_for_stable_ui(lambda: stable_png)
        assert result["stable"] is True
        assert result["elapsed_ms"] >= 300  # default stable_ms

    def test_stable_result_includes_last_frame(self) -> None:
        """Stable result should include the last captured frame bytes."""
        stable_png = _make_png((50, 50, 50))
        result = wait_for_stable_ui(
            lambda: stable_png,
            stable_ms=50,
            timeout_ms=2000,
            poll_ms=10,
        )
        assert "last_frame" in result
        assert result["last_frame"] == stable_png

    def test_last_frame_is_from_final_poll(self) -> None:
        """last_frame should be the bytes from the last poll, not an earlier one."""
        frames = [
            _make_gradient_png(offset=0),
            _make_gradient_png(offset=50),
            _make_gradient_png(offset=100),  # settles here
        ]
        call_count = 0

        def screenshot_fn() -> bytes:
            nonlocal call_count
            call_count += 1
            if call_count <= len(frames):
                return frames[call_count - 1]
            return frames[-1]

        result = wait_for_stable_ui(screenshot_fn, stable_ms=80, timeout_ms=3000, poll_ms=20)
        assert result["stable"] is True
        # last_frame should be the settled frame (offset=100)
        assert result["last_frame"] == frames[-1]

    def test_timeout_includes_last_frame(self) -> None:
        """Even on timeout, last_frame should be present."""
        call_count = 0

        def screenshot_fn() -> bytes:
            nonlocal call_count
            call_count += 1
            return _make_gradient_png(offset=call_count * 30)

        result = wait_for_stable_ui(screenshot_fn, stable_ms=200, timeout_ms=300, poll_ms=20)
        assert result["stable"] is False
        assert "last_frame" in result
        assert isinstance(result["last_frame"], bytes)
        assert len(result["last_frame"]) > 0
