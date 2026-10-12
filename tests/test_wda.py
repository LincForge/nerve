"""Tests for WDA integration — all mocked, no running WDA needed."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import structlog.testing

import linc_nerve.core.wda as wda_mod
from linc_nerve.core.wda import (
    WDAManager,
    WDASession,
    _allocate_port,
    _prepare_xctestrun,
    _release_port,
    _reset_port_state,
    build_wda,
    parse_wda_source,
)

# ---------------------------------------------------------------------------
# WDASession
# ---------------------------------------------------------------------------


class TestWDASession:
    def test_tap_sends_correct_request(self) -> None:
        session = WDASession("UUID-1234", 8100)
        session._session_id = "SID-1234"
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.request.return_value = MagicMock(status_code=200)
        session._client = mock_client

        session.tap(200, 400)

        mock_client.request.assert_called_once_with(
            "POST",
            "/session/SID-1234/wda/tap",
            json={"x": 200, "y": 400},
        )

    def test_swipe_sends_correct_request(self) -> None:
        session = WDASession("UUID-1234", 8100)
        session._session_id = "SID-1234"
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.request.return_value = MagicMock(status_code=200)
        session._client = mock_client

        session.swipe(100, 600, 100, 200, 500)

        mock_client.request.assert_called_once_with(
            "POST",
            "/session/SID-1234/wda/dragfromtoforduration",
            json={
                "fromX": 100,
                "fromY": 600,
                "toX": 100,
                "toY": 200,
                "duration": 0.5,
            },
        )

    def test_source_returns_parsed_json(self) -> None:
        session = WDASession("UUID-1234", 8100)
        session._session_id = "SID-1234"
        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"value": {"type": "Application", "children": []}}
        mock_client.request.return_value = mock_resp
        session._client = mock_client

        result = session.source()
        assert result["value"]["type"] == "Application"

    def test_health_check_success(self) -> None:
        session = WDASession("UUID-1234", 8100)
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = MagicMock(status_code=200)
        session._client = mock_client

        assert session._is_healthy() is True

    def test_health_check_failure(self) -> None:
        session = WDASession("UUID-1234", 8100)
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.side_effect = httpx.ConnectError("refused")
        session._client = mock_client

        assert session._is_healthy() is False

    def test_health_check_no_client(self) -> None:
        session = WDASession("UUID-1234", 8100)
        assert session._is_healthy() is False

    def test_request_retries_on_connection_error(self) -> None:
        session = WDASession("UUID-1234", 8100)
        mock_client = MagicMock(spec=httpx.Client)
        # First call fails, second succeeds (after restart)
        mock_client.request.side_effect = httpx.ConnectError("refused")
        session._client = mock_client

        with patch.object(session, "stop"), patch.object(session, "start") as mock_start:
            # After restart, new client should work
            new_client = MagicMock(spec=httpx.Client)
            new_client.request.return_value = MagicMock(status_code=200)

            def set_new_client() -> None:
                session._client = new_client

            mock_start.side_effect = set_new_client

            resp = session._request("POST", "/wda/tap/0", json={"x": 1, "y": 2})
            assert resp.status_code == 200

    def test_request_recovers_on_404_stale_session(self) -> None:
        session = WDASession("UUID-1234", 8100)
        session._session_id = "SID-1234"
        mock_client = MagicMock(spec=httpx.Client)
        session._client = mock_client

        # Mock the first request to return 404
        mock_resp_404 = MagicMock(status_code=404)
        # Mock the second request (after recreation) to return 200
        mock_resp_200 = MagicMock(status_code=200)
        mock_client.request.side_effect = [mock_resp_404, mock_resp_200]

        with patch.object(session, "_create_session") as mock_create:
            # We mock _create_session to update session_id to SID-5678
            def set_new_session_id() -> None:
                session._session_id = "SID-5678"

            mock_create.side_effect = set_new_session_id

            resp = session._request("POST", "/wda/tap", json={"x": 1, "y": 2})
            assert resp.status_code == 200
            assert session._session_id == "SID-5678"

            # Assert create_session was called
            mock_create.assert_called_once()
            # Assert client.request was called twice:
            # 1. With the old stale session id SID-1234
            # 2. With the new session id SID-5678
            mock_client.request.assert_any_call(
                "POST", "/session/SID-1234/wda/tap", json={"x": 1, "y": 2}
            )
            mock_client.request.assert_any_call(
                "POST", "/session/SID-5678/wda/tap", json={"x": 1, "y": 2}
            )

    def test_http_500_raises(self) -> None:
        session = WDASession("UUID-1234", 8100)
        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock(status_code=500)
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500",
            request=MagicMock(),
            response=mock_resp,
        )
        mock_client.request.return_value = mock_resp
        session._client = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            session.tap(100, 200)

    def test_screen_size(self) -> None:
        session = WDASession("UUID-1234", 8100)
        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"value": {"width": 393, "height": 852}}
        mock_client.request.return_value = mock_resp
        session._client = mock_client

        size = session.screen_size()
        assert size == {"width": 393, "height": 852}

    def test_orientation(self) -> None:
        session = WDASession("UUID-1234", 8100)
        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"value": "PORTRAIT"}
        mock_client.request.return_value = mock_resp
        session._client = mock_client

        assert session.orientation() == "portrait"

    def test_drain_stream_discovers_localhost_url(self) -> None:
        """Thread-based stream reader discovers simulator URL."""
        import io
        import threading

        session = WDASession("UUID-1234", 9999)
        session._url_event = threading.Event()
        stream_data = (
            b"Some output\nServerURLHere->http://localhost:8100<-ServerURLHere\nMore output\n"
        )
        stream = io.BytesIO(stream_data)
        session._drain_stream(stream)

        assert session.host == "localhost"
        assert session.port == 8100

    def test_drain_stream_discovers_device_ip(self) -> None:
        """Thread-based stream reader discovers physical device URL."""
        import io
        import threading

        session = WDASession("UUID-1234", 8100)
        session._url_event = threading.Event()
        stream = io.BytesIO(
            b"xcodebuild noise\nServerURLHere->http://192.0.2.10:8100<-ServerURLHere\n"
        )
        session._drain_stream(stream)

        assert session.host == "192.0.2.10"
        assert session.port == 8100

    def test_drain_stream_no_url_keeps_defaults(self) -> None:
        """No ServerURLHere in output leaves defaults unchanged."""
        import io
        import threading

        session = WDASession("UUID-1234", 8100)
        session._url_event = threading.Event()
        stream = io.BytesIO(b"Some xcodebuild output\nTest started\n")
        session._drain_stream(stream)

        assert session.host == "localhost"
        assert session.port == 8100


# ---------------------------------------------------------------------------
# WDAManager
# ---------------------------------------------------------------------------


class TestWDAManager:
    @patch("linc_nerve.core.wda.WDAManager._cleanup_orphans")
    def test_stop_removes_session(self, _orphans: MagicMock) -> None:
        mgr = WDAManager()
        mock_session = MagicMock(spec=WDASession)
        mgr._sessions["UUID-1234"] = mock_session

        mgr.stop("UUID-1234")

        mock_session.stop.assert_called_once()
        assert "UUID-1234" not in mgr._sessions

    @patch("linc_nerve.core.wda.WDAManager._cleanup_orphans")
    def test_stop_all_cleans_everything(self, _orphans: MagicMock) -> None:
        mgr = WDAManager()
        s1 = MagicMock(spec=WDASession)
        s2 = MagicMock(spec=WDASession)
        mgr._sessions = {"A": s1, "B": s2}

        mgr.stop_all()

        s1.stop.assert_called_once()
        s2.stop.assert_called_once()
        assert mgr._sessions == {}


# ---------------------------------------------------------------------------
# Source Parsing
# ---------------------------------------------------------------------------


class TestParseWDASource:
    def test_maps_wda_fields_to_element_dict(self) -> None:
        """Test with real WDA format (short types, frame strings, rawIdentifier)."""
        tree = {
            "type": "Application",
            "label": "iosApp",
            "children": [
                {
                    "type": "Button",
                    "rawIdentifier": "submitBtn",
                    "label": "Submit",
                    "value": None,
                    "frame": "{{100, 200}, {200, 50}}",
                    "isEnabled": "1",
                    "isVisible": "1",
                    "children": [],
                },
            ],
        }

        result = parse_wda_source(tree)
        assert len(result) == 1

        elem = result[0]
        assert elem["id"] == "submitBtn"
        assert elem["text"] == "Submit"
        assert elem["class"] == "Button"
        assert elem["bounds"] == "[100,200][300,250]"
        assert elem["clickable"] is True

    def test_bounds_from_frame_string(self) -> None:
        tree = {
            "type": "Application",
            "children": [
                {
                    "type": "StaticText",
                    "rawIdentifier": "title",
                    "label": "Hello",
                    "frame": "{{10, 20}, {100, 50}}",
                    "children": [],
                },
            ],
        }

        result = parse_wda_source(tree)
        assert result[0]["bounds"] == "[10,20][110,70]"

    def test_bounds_from_rect_dict(self) -> None:
        """Some WDA versions use rect dict instead of frame string."""
        tree = {
            "type": "Application",
            "children": [
                {
                    "type": "StaticText",
                    "name": "title",
                    "label": "Hello",
                    "rect": {"x": 10, "y": 20, "width": 100, "height": 50},
                    "children": [],
                },
            ],
        }

        result = parse_wda_source(tree)
        assert result[0]["bounds"] == "[10,20][110,70]"

    def test_max_depth_respected(self) -> None:
        # Build a tree of depth 15 — use non-container types with labels
        node: dict = {"type": "Button", "label": "deep", "children": []}
        for i in range(15):
            node = {"type": "ScrollView", "label": f"wrap{i}", "children": [node]}

        result = parse_wda_source(node, max_depth=3)
        assert len(result) < 15

    def test_max_elements_cap(self) -> None:
        children = [
            {
                "type": "Button",
                "rawIdentifier": f"btn_{i}",
                "label": f"Button {i}",
                "frame": f"{{{{0, {i * 50}}}, {{100, 50}}}}",
                "children": [],
            }
            for i in range(100)
        ]
        tree = {"type": "Application", "children": children}

        result = parse_wda_source(tree, max_elements=20)
        assert len(result) == 20

    def test_skips_container_elements(self) -> None:
        tree = {
            "type": "Application",
            "children": [
                {
                    "type": "Other",
                    "rawIdentifier": "",
                    "label": "",
                    "value": None,
                    "frame": "{{0, 0}, {100, 100}}",
                    "isEnabled": "1",
                    "children": [],
                },
                {
                    "type": "Button",
                    "rawIdentifier": "ok",
                    "label": "OK",
                    "frame": "{{50, 50}, {100, 50}}",
                    "isEnabled": "1",
                    "children": [],
                },
            ],
        }

        result = parse_wda_source(tree)
        assert len(result) == 1
        assert result[0]["text"] == "OK"

    def test_missing_fields_handled_gracefully(self) -> None:
        tree = {
            "type": "Application",
            "children": [
                {
                    "type": "Button",
                    "label": "Tap me",
                    # rawIdentifier, value, frame all missing
                    "children": [],
                },
            ],
        }

        result = parse_wda_source(tree)
        assert len(result) == 1
        assert result[0]["text"] == "Tap me"
        # No frame → empty bounds → omitted from compact dict
        assert "bounds" not in result[0]

    def test_value_used_when_label_empty(self) -> None:
        tree = {
            "type": "Application",
            "children": [
                {
                    "type": "TextField",
                    "rawIdentifier": "email",
                    "label": "",
                    "value": "user@example.com",
                    "frame": "{{0, 0}, {300, 40}}",
                    "isEnabled": "1",
                    "children": [],
                },
            ],
        }

        result = parse_wda_source(tree)
        assert result[0]["text"] == "user@example.com"

    def test_string_boolean_handling(self) -> None:
        """WDA returns isEnabled as '0'/'1' strings, not booleans."""
        tree = {
            "type": "Application",
            "children": [
                {
                    "type": "Button",
                    "label": "Disabled",
                    "isEnabled": "0",
                    "children": [],
                },
            ],
        }

        result = parse_wda_source(tree)
        assert len(result) == 1
        assert result[0].get("enabled") is False
        assert "clickable" not in result[0]  # Disabled → not clickable


# ---------------------------------------------------------------------------
# Port Allocator
# ---------------------------------------------------------------------------


@pytest.fixture()
def _reset_ports() -> None:
    """Reset module-level port allocation state for test isolation."""
    _reset_port_state()
    yield
    _reset_port_state()


@pytest.mark.usefixtures("_reset_ports")
class TestPortAllocator:
    def test_first_port_is_8100(self) -> None:
        assert _allocate_port("sim-A") == 8100

    def test_allocate_unique_per_serial(self) -> None:
        p1 = _allocate_port("sim-A")
        p2 = _allocate_port("sim-B")
        assert p1 != p2
        assert p1 == 8100
        assert p2 == 8101

    def test_same_serial_returns_same_port(self) -> None:
        p1 = _allocate_port("sim-A")
        p2 = _allocate_port("sim-A")
        assert p1 == p2

    def test_release_recycles_port(self) -> None:
        _allocate_port("sim-A")
        p2 = _allocate_port("sim-B")
        _allocate_port("sim-C")
        _release_port("sim-B")
        # Released port (8101) should be recycled for the next allocation
        p4 = _allocate_port("sim-D")
        assert p4 == p2  # recycled 8101, not 8103

    def test_release_recycles_lowest_free(self) -> None:
        _allocate_port("sim-A")  # 8100
        _allocate_port("sim-B")  # 8101
        _allocate_port("sim-C")  # 8102
        _release_port("sim-C")  # free 8102
        _release_port("sim-A")  # free 8100
        # Should recycle 8100 (lowest) first
        assert _allocate_port("sim-D") == 8100
        assert _allocate_port("sim-E") == 8102

    def test_concurrent_allocation_no_duplicates(self) -> None:
        import threading

        results: list[int] = []
        lock = threading.Lock()

        def alloc(serial: str) -> None:
            port = _allocate_port(serial)
            with lock:
                results.append(port)

        threads = [threading.Thread(target=alloc, args=(f"sim-{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert len(set(results)) == 10  # no duplicates


# ---------------------------------------------------------------------------
# Plist Injection
# ---------------------------------------------------------------------------


class TestPrepareXctestrun:
    def test_injects_use_port(self, tmp_path: Path) -> None:
        import plistlib

        plist_data = {
            "WebDriverAgentRunner": {
                "TestHostBundleIdentifier": "com.apple.test.WDA",
            },
        }
        plist_file = tmp_path / "test.xctestrun"
        with open(plist_file, "wb") as f:
            plistlib.dump(plist_data, f)

        result = _prepare_xctestrun(plist_file, 8102)
        try:
            with open(result, "rb") as f:
                modified = plistlib.load(f)
            env = modified["WebDriverAgentRunner"]["EnvironmentVariables"]
            assert env["USE_PORT"] == "8102"
        finally:
            result.unlink()

    def test_preserves_existing_env_vars(self, tmp_path: Path) -> None:
        plist_data = {
            "WebDriverAgentRunner": {
                "EnvironmentVariables": {
                    "EXISTING_VAR": "keep_me",
                },
            },
        }
        plist_file = tmp_path / "test.xctestrun"
        import plistlib

        with open(plist_file, "wb") as f:
            plistlib.dump(plist_data, f)

        result = _prepare_xctestrun(plist_file, 8103)
        try:
            with open(result, "rb") as f:
                modified = plistlib.load(f)
            env = modified["WebDriverAgentRunner"]["EnvironmentVariables"]
            assert env["USE_PORT"] == "8103"
            assert env["EXISTING_VAR"] == "keep_me"
        finally:
            result.unlink()


# ---------------------------------------------------------------------------
# Manager Stop Releases Port
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_reset_ports")
class TestWDAManagerPortRelease:
    @patch("linc_nerve.core.wda.WDAManager._cleanup_orphans")
    def test_stop_releases_port(self, _orphans: MagicMock) -> None:
        import linc_nerve.core.wda as wda_mod

        mgr = WDAManager()
        mock_session = MagicMock(spec=WDASession)
        mgr._sessions["sim-A"] = mock_session
        wda_mod._port_allocations["sim-A"] = 8100

        mgr.stop("sim-A")

        assert "sim-A" not in wda_mod._port_allocations

    @patch("linc_nerve.core.wda.WDAManager._cleanup_orphans")
    def test_stop_all_releases_ports(self, _orphans: MagicMock) -> None:
        import linc_nerve.core.wda as wda_mod

        mgr = WDAManager()
        mgr._sessions = {"A": MagicMock(spec=WDASession), "B": MagicMock(spec=WDASession)}
        wda_mod._port_allocations["A"] = 8100
        wda_mod._port_allocations["B"] = 8101

        mgr.stop_all()

        assert "A" not in wda_mod._port_allocations
        assert "B" not in wda_mod._port_allocations


# ---------------------------------------------------------------------------
# Port Exhaustion & Bounded Range
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_reset_ports")
class TestPortExhaustion:
    def test_port_exhaustion_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pool raises RuntimeError when all ports are allocated."""
        monkeypatch.setattr(wda_mod, "_WDA_PORT_MAX", wda_mod._WDA_PORT_MIN + 2)
        _allocate_port("sim-A")  # 8100
        _allocate_port("sim-B")  # 8101
        _allocate_port("sim-C")  # 8102
        with pytest.raises(RuntimeError, match="port pool exhausted"):
            _allocate_port("sim-D")

    def test_port_range_bounded_after_recycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After exhaustion, releasing a port allows reuse within bounds."""
        monkeypatch.setattr(wda_mod, "_WDA_PORT_MAX", wda_mod._WDA_PORT_MIN + 2)
        _allocate_port("sim-A")  # 8100
        _allocate_port("sim-B")  # 8101
        _allocate_port("sim-C")  # 8102
        _release_port("sim-B")  # free 8101
        recycled = _allocate_port("sim-D")
        assert recycled == 8101  # recycled, not 8103


# ---------------------------------------------------------------------------
# Sweep Dead Ports
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_reset_ports")
class TestSweepDeadPorts:
    @patch("linc_nerve.core.wda.subprocess.run")
    @patch("linc_nerve.core.wda.WDAManager._cleanup_orphans")
    def test_sweep_resets_when_no_survivors(
        self, _orphans: MagicMock, mock_run: MagicMock
    ) -> None:
        """When no WDA processes survive orphan cleanup, port state is reset."""
        # Pre-populate port state as if a previous session allocated ports
        wda_mod._port_allocations["sim-A"] = 8100
        wda_mod._next_port = 8101

        # pgrep returns non-zero (no processes found)
        mock_run.return_value = MagicMock(returncode=1)

        WDAManager()

        assert wda_mod._port_allocations == {}
        assert wda_mod._next_port == 8100
        assert wda_mod._free_ports == set()

    @patch("linc_nerve.core.wda.subprocess.run")
    @patch("linc_nerve.core.wda.WDAManager._cleanup_orphans")
    def test_sweep_preserves_when_survivors_exist(
        self, _orphans: MagicMock, mock_run: MagicMock
    ) -> None:
        """When WDA processes survive, port state is preserved."""
        wda_mod._port_allocations["sim-A"] = 8100
        wda_mod._next_port = 8101

        # pgrep returns 0 (processes found)
        mock_run.return_value = MagicMock(returncode=0)

        WDAManager()

        assert wda_mod._port_allocations == {"sim-A": 8100}
        assert wda_mod._next_port == 8101


# ---------------------------------------------------------------------------
# WDA process pattern — real cmdline shape (#136)
# ---------------------------------------------------------------------------

# Observed live (PID 41991, 2026-09-09, on a local dev checkout): the
# actual process xcodebuild spawns for a WDA runner. There is no literal
# "WebDriverAgent" anywhere in it — the .xctestrun file is a randomly-named
# tmp file under the WDA build dir.
#
# The build-dir prefix is derived from ``wda_mod._WDA_BUILD_DIR`` (the same
# HOME-relative constant ``build_wda``/``_find_xctestrun`` resolve the real
# xctestrun path from, wda.py:30 + wda_build.py) rather than hardcoded to
# a single user's home directory — the original literal only matched on the machine it was
# captured on. ``_wda_process_pattern()`` computes its pattern from that same
# constant at test-run time (wda.py:135), so a real runner cmdline on ANY
# host is always rooted under ``_WDA_BUILD_DIR`` for THAT host; hardcoding
# one host's expansion made this fixture non-portable and it broke on CI's
# ubuntu-latest runner (HOME=/home/runner) while passing on the dev laptop —
# the pattern logic itself was always correct (#136 repair round).
_REAL_WDA_RUNNER_CMDLINE = (
    "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild "
    "test-without-building -xctestrun "
    f"{wda_mod._WDA_BUILD_DIR}/Build/Products/tmpouecp3jd.xctestrun "
    "-destination platform=iOS Simulator,id=9397270F-E76A-4136-869F-0F3EC32B1F4C"
)

_OLD_BUGGY_PATTERN = "xcodebuild.*WebDriverAgent"


class TestWdaProcessPattern:
    def test_old_pattern_never_matched_the_real_runner_cmdline(self) -> None:
        """Characterizes the bug: the pre-fix literal pattern has no path to
        match the real runner cmdline (no 'WebDriverAgent' substring exists
        in it), so orphan cleanup silently reaped nothing.
        """
        assert re.search(_OLD_BUGGY_PATTERN, _REAL_WDA_RUNNER_CMDLINE) is None

    def test_shared_pattern_matches_the_real_runner_cmdline(self) -> None:
        """The detector used by cleanup/sweep must select the real runner
        cmdline shape. RED against the old code (no `_wda_process_pattern`
        helper exists yet / matches on the dead 'WebDriverAgent' literal).
        """
        pattern = wda_mod._wda_process_pattern()
        assert re.search(pattern, _REAL_WDA_RUNNER_CMDLINE) is not None

    def test_shared_pattern_does_not_match_unrelated_xcodebuild_invocations(
        self,
    ) -> None:
        """A plain (non-WDA) xcodebuild invocation must not be swept up."""
        unrelated = (
            "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild "
            "build -scheme SomeOtherApp -destination platform=iOS Simulator"
        )
        pattern = wda_mod._wda_process_pattern()
        assert re.search(pattern, unrelated) is None

    def test_shared_pattern_does_not_match_the_build_step_for_the_same_app(
        self,
    ) -> None:
        """Regression (#136 repair round 0): the build-for-testing step that
        PRODUCES the xctestrun under ``_WDA_BUILD_DIR`` must NOT match — only
        the runner invocation that later CONSUMES it via ``-xctestrun``
        should. Pre-fix, the pattern was ``xcodebuild.*<build dir>`` with no
        ``test-without-building``/``-xctestrun`` discriminator, so this
        build-time cmdline (which also references the build dir via
        ``-derivedDataPath``) matched identically to the real runner —
        meaning an in-flight WDA build could be SIGTERMed by
        ``_cleanup_orphans``'s unconditional ``pkill``.
        """
        build_step_cmdline = (
            "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild "
            "build-for-testing -derivedDataPath "
            f"{wda_mod._WDA_BUILD_DIR} -scheme WebDriverAgentRunner "
            "-destination platform=iOS Simulator,id=9397270F-E76A-4136-869F-0F3EC32B1F4C"
        )
        pattern = wda_mod._wda_process_pattern()
        assert re.search(pattern, build_step_cmdline) is None

    @patch("linc_nerve.core.wda.subprocess.run")
    def test_cleanup_orphans_pgreps_using_the_shared_pattern(self, mock_run: MagicMock) -> None:
        """_cleanup_orphans must enumerate candidates with `pgrep -lf` using
        the SAME pattern the sweep uses — not a hand-rolled duplicate that
        can drift, and not a blind `pkill -f` (#142 round 1: a bare pkill
        cannot discriminate a live sibling runner from a genuine orphan).
        """
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        WDAManager._cleanup_orphans()

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "pgrep"
        assert args[1] == "-lf"
        assert args[2] == wda_mod._wda_process_pattern()

    @pytest.mark.usefixtures("_reset_ports")
    @patch("linc_nerve.core.wda.subprocess.run")
    @patch("linc_nerve.core.wda.WDAManager._cleanup_orphans")
    def test_sweep_dead_ports_pgreps_using_the_shared_pattern(
        self, _orphans: MagicMock, mock_run: MagicMock
    ) -> None:
        """_sweep_dead_ports must query with the SAME pattern _cleanup_orphans
        kills with, so the sweep's verdict can never disagree with what
        cleanup actually targeted.
        """
        mock_run.return_value = MagicMock(returncode=1)

        WDAManager()

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "pgrep"
        assert args[1] == "-f"
        assert args[2] == wda_mod._wda_process_pattern()

    @pytest.mark.usefixtures("_reset_ports")
    @patch("linc_nerve.core.wda.subprocess.run")
    @patch("linc_nerve.core.wda.WDAManager._cleanup_orphans")
    def test_sweep_does_not_falsely_report_reclaimed_all_while_wda_process_alive(
        self, _orphans: MagicMock, mock_run: MagicMock
    ) -> None:
        """Regression for the live incident: with the real runner cmdline
        alive, the sweep must NOT log wda_ports_swept(reclaimed=all) or call
        _reset_port_state.

        Drives the REAL detector rather than hard-coding pgrep's answer: the
        fake ``subprocess.run`` applies whatever pattern ``_sweep_dead_ports``
        actually passed (``argv[2]``) to the real runner cmdline fixture and
        computes pgrep's exit code from that, so this test is RED under the
        pre-fix pattern (which misses the real cmdline -> returncode 1 ->
        ``reclaimed=all`` gets logged and ``_reset_port_state()`` runs) and
        GREEN under the fixed, narrowed pattern.
        """
        wda_mod._port_allocations["sim-A"] = 8100
        wda_mod._next_port = 8101

        def fake_pgrep(argv: list[str], **_kwargs: object) -> MagicMock:
            pattern = argv[2]
            matched = re.search(pattern, _REAL_WDA_RUNNER_CMDLINE) is not None
            return MagicMock(returncode=0 if matched else 1)

        mock_run.side_effect = fake_pgrep

        with structlog.testing.capture_logs() as cap_logs:
            WDAManager()

        assert wda_mod._port_allocations == {"sim-A": 8100}
        assert wda_mod._next_port == 8101
        reclaimed_events = [
            e
            for e in cap_logs
            if e.get("event") == "wda_ports_swept" and e.get("reclaimed") == "all"
        ]
        assert reclaimed_events == []
        skipped_events = [e for e in cap_logs if e.get("event") == "wda_sweep_skipped"]
        assert len(skipped_events) == 1

    @pytest.mark.usefixtures("_reset_ports")
    @patch("linc_nerve.core.wda.os.kill")
    @patch("linc_nerve.core.wda._runner_parents", return_value={111: 1, 222: 1})
    @patch("linc_nerve.core.wda.subprocess.run")
    def test_cleanup_orphans_does_not_kill_a_runner_whose_udid_is_live(
        self, mock_run: MagicMock, _parents: MagicMock, mock_kill: MagicMock
    ) -> None:
        """Regression (#142 round 1): a runner whose destination udid is a
        CURRENTLY-ALLOCATED device in ``_port_allocations`` is LIVE — this
        process's own session, or a concurrent nerve process's sharing the
        same host-global ``_WDA_BUILD_DIR`` — and must never be killed by
        orphan cleanup, even though its cmdline matches
        ``_wda_process_pattern()`` (that pattern discriminates runner-vs-
        build only, not orphan-vs-live). A genuine orphan (udid NOT
        allocated) must still be killed.
        """
        live_udid = "9397270F-E76A-4136-869F-0F3EC32B1F4C"
        orphan_udid = "DEADBEEF-0000-0000-0000-000000000000"
        live_cmdline = _REAL_WDA_RUNNER_CMDLINE  # already carries live_udid
        orphan_cmdline = (
            "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild "
            "test-without-building -xctestrun "
            f"{wda_mod._WDA_BUILD_DIR}/Build/Products/tmpstalexx.xctestrun "
            f"-destination platform=iOS Simulator,id={orphan_udid}"
        )
        wda_mod._port_allocations[live_udid] = 8100

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"111 {live_cmdline}\n222 {orphan_cmdline}\n",
        )

        WDAManager._cleanup_orphans()

        args = mock_run.call_args[0][0]
        assert args[0] == "pgrep"
        assert args[1] == "-lf"
        assert args[2] == wda_mod._wda_process_pattern()
        mock_kill.assert_called_once()
        assert mock_kill.call_args[0][0] == 222

    @pytest.mark.usefixtures("_reset_ports")
    @patch("linc_nerve.core.wda.os.kill")
    @patch("linc_nerve.core.wda.subprocess.run")
    def test_sweep_dead_ports_leaves_state_intact_when_a_live_sibling_remains(
        self, mock_run: MagicMock, mock_kill: MagicMock
    ) -> None:
        """End-to-end regression (#142 round 1): with the REAL
        ``_cleanup_orphans`` sparing a live sibling runner and killing a
        genuine orphan, the follow-up ``_sweep_dead_ports`` must still see
        the live sibling's cmdline in its own ``pgrep -f`` query and leave
        port state alone — never call ``_reset_port_state()`` or log
        ``wda_ports_swept(reclaimed=all)``.
        """
        live_udid = "9397270F-E76A-4136-869F-0F3EC32B1F4C"
        orphan_udid = "DEADBEEF-0000-0000-0000-000000000000"
        live_cmdline = _REAL_WDA_RUNNER_CMDLINE
        orphan_cmdline = (
            "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild "
            "test-without-building -xctestrun "
            f"{wda_mod._WDA_BUILD_DIR}/Build/Products/tmpstalexy.xctestrun "
            f"-destination platform=iOS Simulator,id={orphan_udid}"
        )
        wda_mod._port_allocations[live_udid] = 8100
        wda_mod._next_port = 8101

        def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
            if argv[0] == "pgrep" and argv[1] == "-lf":
                return MagicMock(
                    returncode=0,
                    stdout=f"111 {live_cmdline}\n222 {orphan_cmdline}\n",
                )
            if argv[0] == "pgrep" and argv[1] == "-f":
                # The orphan (222) has been reaped by cleanup; only the
                # live runner (111) still matches this query.
                return MagicMock(returncode=0)
            if argv[0] == "ps":
                # Both runners re-parented to launchd: the udid check alone
                # must still spare the live, allocated one.
                return MagicMock(returncode=0, stdout="  111     1\n  222     1\n")
            raise AssertionError(f"unexpected subprocess.run call: {argv}")

        mock_run.side_effect = fake_run

        with structlog.testing.capture_logs() as cap_logs:
            WDAManager()

        mock_kill.assert_called_once()
        assert mock_kill.call_args[0][0] == 222
        assert wda_mod._port_allocations == {live_udid: 8100}
        assert wda_mod._next_port == 8101
        reclaimed_events = [
            e
            for e in cap_logs
            if e.get("event") == "wda_ports_swept" and e.get("reclaimed") == "all"
        ]
        assert reclaimed_events == []


# ---------------------------------------------------------------------------
# Live-sibling protection under REAL macOS (BSD) pgrep semantics (#142 (b))
# ---------------------------------------------------------------------------

_LIVE_UDID = "9397270F-E76A-4136-869F-0F3EC32B1F4C"
_ORPHAN_UDID = "DEADBEEF-0000-0000-0000-000000000000"


def _runner_cmdline(udid: str, tmp_name: str = "tmpouecp3jd") -> str:
    return (
        "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild "
        "test-without-building -xctestrun "
        f"{wda_mod._WDA_BUILD_DIR}/Build/Products/{tmp_name}.xctestrun "
        f"-destination platform=iOS Simulator,id={udid}"
    )


class _FakeBsdProcessTable:
    """A fake process table whose ``pgrep`` behaves like macOS/BSD pgrep —
    the only platform a WDA runner ever exists on.

    BSD semantics (``man pgrep`` on macOS), which differ from Linux procps:

    * ``-f`` matches the pattern against the full argument list.
    * ``-l`` with ``-f`` prints ``<pid> <full argument list>`` per match.
    * ``-a`` means "include process ANCESTORS in the match list" — it does
      NOT print the command line. ``pgrep -af`` therefore prints bare PIDs.

    ``kill`` removes the pid, so a follow-up ``pgrep -f`` (the sweep) sees
    exactly the processes cleanup left alive.

    ``ps -o pid=,ppid= -p <pids>`` answers from ``parents`` (default ppid 1:
    re-parented to launchd, i.e. the spawning nerve process is gone).
    """

    def __init__(self, procs: dict[int, str], parents: dict[int, int] | None = None) -> None:
        self.procs = dict(procs)
        self.parents = dict(parents or {})
        self.killed: list[int] = []

    def _ps(self, argv: list[str]) -> MagicMock:
        assert argv[:4] == ["ps", "-o", "pid=,ppid=", "-p"], argv
        pids = [int(p) for p in argv[4].split(",") if int(p) in self.procs]
        if not pids:
            return MagicMock(returncode=1, stdout="")
        out = "".join(f"{pid:>5} {self.parents.get(pid, 1):>5}\n" for pid in pids)
        return MagicMock(returncode=0, stdout=out)

    def run(self, argv: list[str], **_kwargs: object) -> MagicMock:
        if argv[0] == "ps":
            return self._ps(argv)
        assert argv[0] == "pgrep", f"unexpected subprocess.run call: {argv}"
        flags, pattern = argv[1], argv[2]
        assert "f" in flags, "WDA detectors must match the full argv (-f)"
        hits = [(pid, cmd) for pid, cmd in sorted(self.procs.items()) if re.search(pattern, cmd)]
        if not hits:
            return MagicMock(returncode=1, stdout="")
        if "l" in flags:
            out = "".join(f"{pid} {cmd}\n" for pid, cmd in hits)
        else:
            out = "".join(f"{pid}\n" for pid, _ in hits)
        return MagicMock(returncode=0, stdout=out)

    def kill(self, pid: int, _sig: int) -> None:
        if pid not in self.procs:
            raise ProcessLookupError(pid)
        del self.procs[pid]
        self.killed.append(pid)


@pytest.mark.usefixtures("_reset_ports")
class TestLiveSiblingSurvivesOnMacOSPgrep:
    def test_new_manager_spares_live_sibling_and_keeps_its_ports(self) -> None:
        """MINOS #142 round-1 finding (b), under the pgrep that actually runs.

        A live runner for an allocated udid (111) and a genuine orphan (222)
        both match ``_wda_process_pattern()``. Constructing a new
        ``WDAManager()`` must reap ONLY the orphan, and the follow-up
        ``_sweep_dead_ports`` must then see the surviving sibling and leave
        its port allocation intact.

        RED on PR head 1d2f153: it enumerates with ``pgrep -af``, which on
        macOS prints bare PIDs (``-a`` = include ancestors), so every
        cmdline parses as empty, no udid is found, and the live sibling is
        SIGTERMed — after which the sweep sees no survivors and wipes its
        port state. The Linux-shaped ``"<pid> <cmdline>"`` stdout the
        earlier tests hand-feed never occurs on the platform WDA runs on.
        """
        table = _FakeBsdProcessTable(
            {
                111: _runner_cmdline(_LIVE_UDID),
                222: _runner_cmdline(_ORPHAN_UDID, "tmpstalexx"),
            }
        )
        wda_mod._port_allocations[_LIVE_UDID] = 8100
        wda_mod._next_port = 8101

        with (
            patch("linc_nerve.core.wda.subprocess.run", side_effect=table.run),
            patch("linc_nerve.core.wda.os.kill", side_effect=table.kill),
            structlog.testing.capture_logs() as cap_logs,
        ):
            WDAManager()

        assert table.killed == [222]
        assert 111 in table.procs
        assert wda_mod._port_allocations == {_LIVE_UDID: 8100}
        assert wda_mod._next_port == 8101
        assert not [e for e in cap_logs if e.get("event") == "wda_ports_swept"]

    def test_logs_cleaned_only_when_an_orphan_was_actually_signalled(self) -> None:
        orphan_only = _FakeBsdProcessTable({222: _runner_cmdline(_ORPHAN_UDID)})
        with (
            patch("linc_nerve.core.wda.subprocess.run", side_effect=orphan_only.run),
            patch("linc_nerve.core.wda.os.kill", side_effect=orphan_only.kill),
            structlog.testing.capture_logs() as cap_logs,
        ):
            WDAManager._cleanup_orphans()
        assert orphan_only.killed == [222]
        assert [e["event"] for e in cap_logs] == ["wda_orphans_cleaned"]

        wda_mod._port_allocations[_LIVE_UDID] = 8100
        live_only = _FakeBsdProcessTable({111: _runner_cmdline(_LIVE_UDID)})
        with (
            patch("linc_nerve.core.wda.subprocess.run", side_effect=live_only.run),
            patch("linc_nerve.core.wda.os.kill", side_effect=live_only.kill),
            structlog.testing.capture_logs() as cap_logs,
        ):
            WDAManager._cleanup_orphans()
        assert live_only.killed == []
        assert cap_logs == []


class TestParsePgrepLongOutput:
    def test_parses_pid_and_full_argv_preserving_spaces(self) -> None:
        live = _runner_cmdline(_LIVE_UDID)
        out = f"111 {live}\n222 /usr/bin/xcodebuild a  b\n"
        assert wda_mod._parse_pgrep_long_output(out) == [
            (111, live),
            (222, "/usr/bin/xcodebuild a  b"),
        ]

    def test_skips_blank_and_non_numeric_lines_and_strips_padding(self) -> None:
        out = "\n   \nabc xcodebuild\n-1 xcodebuild\n  333 xcodebuild x  \n"
        assert wda_mod._parse_pgrep_long_output(out) == [(333, "xcodebuild x")]

    def test_bare_pid_line_yields_empty_cmdline(self) -> None:
        # What macOS `pgrep -af` prints: a pid with no argv at all.
        assert wda_mod._parse_pgrep_long_output("444\n") == [(444, "")]

    def test_empty_output(self) -> None:
        assert wda_mod._parse_pgrep_long_output("") == []


def _dead_pid() -> int:
    """A pid that provably existed and is now gone (spawned, then waited)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class TestSelectOrphanPids:
    def test_live_sibling_is_not_selected_but_orphan_is(self) -> None:
        candidates = [
            (111, _runner_cmdline(_LIVE_UDID)),
            (222, _runner_cmdline(_ORPHAN_UDID)),
        ]
        parents = {111: 1, 222: 1}
        assert wda_mod._select_orphan_pids(candidates, {_LIVE_UDID}, parents) == [222]

    def test_every_parseable_runner_is_an_orphan_when_nothing_is_allocated(self) -> None:
        candidates = [
            (111, _runner_cmdline(_LIVE_UDID)),
            (222, _runner_cmdline(_ORPHAN_UDID)),
        ]
        parents = {111: 1, 222: 1}
        assert wda_mod._select_orphan_pids(candidates, set(), parents) == [111, 222]

    def test_unparseable_cmdline_is_never_selected_fail_closed(self) -> None:
        candidates = [
            (444, ""),
            (555, f"xcodebuild test-without-building -xctestrun {wda_mod._WDA_BUILD_DIR}/x"),
        ]
        assert wda_mod._select_orphan_pids(candidates, set(), {444: 1, 555: 1}) == []

    def test_physical_device_destination_shape_is_discriminated(self) -> None:
        serial = "00008110-000A1B2C3D4E801E"
        cmd = _runner_cmdline(serial).replace("platform=iOS Simulator", "platform=iOS")
        assert wda_mod._select_orphan_pids([(666, cmd)], {serial}, {666: 1}) == []
        assert wda_mod._select_orphan_pids([(666, cmd)], set(), {666: 1}) == [666]

    def test_no_candidates(self) -> None:
        assert wda_mod._select_orphan_pids([], {_LIVE_UDID}, {}) == []

    # -- audit F3: the udid check separates only THIS process's runners -----

    def test_foreign_udid_runner_with_live_parent_is_a_sibling_not_an_orphan(self) -> None:
        """A runner whose udid this process never allocated may belong to the
        daemon or a sibling bellows. Its parent is alive, so it is not an
        orphan (audit F3; pre-fix it was selected and SIGTERMed)."""
        candidates = [(111, _runner_cmdline(_ORPHAN_UDID))]
        assert wda_mod._select_orphan_pids(candidates, set(), parents={111: os.getpid()}) == []

    def test_foreign_udid_runner_reparented_to_launchd_is_an_orphan(self) -> None:
        candidates = [(111, _runner_cmdline(_ORPHAN_UDID))]
        assert wda_mod._select_orphan_pids(candidates, set(), parents={111: 1}) == [111]

    def test_foreign_udid_runner_with_dead_parent_is_an_orphan(self) -> None:
        candidates = [(111, _runner_cmdline(_ORPHAN_UDID))]
        dead = _dead_pid()
        assert wda_mod._select_orphan_pids(candidates, set(), parents={111: dead}) == [111]

    def test_pid_with_unknown_parent_is_never_selected_fail_closed(self) -> None:
        candidates = [(111, _runner_cmdline(_ORPHAN_UDID))]
        assert wda_mod._select_orphan_pids(candidates, set(), parents={}) == []


class TestParentGone:
    def test_launchd_parent_means_gone(self) -> None:
        assert wda_mod._parent_gone(1) is True

    def test_non_positive_ppid_means_gone(self) -> None:
        assert wda_mod._parent_gone(0) is True

    def test_live_parent_is_not_gone(self) -> None:
        assert wda_mod._parent_gone(os.getpid()) is False

    def test_dead_parent_is_gone(self) -> None:
        assert wda_mod._parent_gone(_dead_pid()) is True

    @patch("linc_nerve.core.wda.pid_alive", return_value=True)
    def test_delegates_liveness_to_the_shared_predicate(self, mock_alive: MagicMock) -> None:
        assert wda_mod._parent_gone(4242) is False
        mock_alive.assert_called_once_with(4242)


class TestParsePsPidPpid:
    def test_parses_padded_pid_ppid_columns(self) -> None:
        assert wda_mod._parse_ps_pid_ppid("  111     1\n  222 4242\n") == {111: 1, 222: 4242}

    def test_skips_garbage_lines(self) -> None:
        out = "\n   \nPID PPID\nabc 1\n333\n444 x\n555 6 7\n  666   7  \n"
        assert wda_mod._parse_ps_pid_ppid(out) == {666: 7}

    def test_empty_output(self) -> None:
        assert wda_mod._parse_ps_pid_ppid("") == {}


class TestRunnerParents:
    @patch("linc_nerve.core.wda.subprocess.run")
    def test_one_ps_call_with_fixed_argv(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="  111     1\n  222 4242\n")
        assert wda_mod._runner_parents([111, 222]) == {111: 1, 222: 4242}
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == ["ps", "-o", "pid=,ppid=", "-p", "111,222"]
        assert mock_run.call_args.kwargs["timeout"] == 5
        assert mock_run.call_args.kwargs.get("shell", False) is False

    @patch("linc_nerve.core.wda.subprocess.run")
    def test_nonzero_exit_yields_empty_map(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="  111     1\n")
        with structlog.testing.capture_logs() as cap_logs:
            assert wda_mod._runner_parents([111, 222]) == {}
        assert [e["event"] for e in cap_logs] == ["wda_orphan_scan_skipped"]

    @pytest.mark.parametrize(
        "exc",
        [subprocess.TimeoutExpired("ps", 5), FileNotFoundError("ps")],
    )
    @patch("linc_nerve.core.wda.subprocess.run")
    def test_ps_hung_or_missing_yields_empty_map(
        self, mock_run: MagicMock, exc: Exception
    ) -> None:
        mock_run.side_effect = exc
        with structlog.testing.capture_logs() as cap_logs:
            assert wda_mod._runner_parents([111, 222]) == {}
        assert [e["event"] for e in cap_logs] == ["wda_orphan_scan_skipped"]

    @patch("linc_nerve.core.wda.subprocess.run")
    def test_empty_input_runs_nothing(self, mock_run: MagicMock) -> None:
        with structlog.testing.capture_logs() as cap_logs:
            assert wda_mod._runner_parents([]) == {}
        mock_run.assert_not_called()
        assert [e["event"] for e in cap_logs] == ["wda_orphan_scan_skipped"]


@pytest.mark.usefixtures("_reset_ports")
class TestCleanupOrphansSparesForeignLiveRunners:
    @patch("linc_nerve.core.wda.os.kill")
    @patch("linc_nerve.core.wda._runner_parents")
    @patch("linc_nerve.core.wda._enumerate_wda_runners")
    def test_sigterms_only_the_runner_whose_parent_is_gone(
        self, mock_enum: MagicMock, mock_parents: MagicMock, mock_kill: MagicMock
    ) -> None:
        """Audit F3 end to end: two runners with udids this process never
        allocated. 111's parent is alive (a sibling daemon/bellows owns it);
        222 was re-parented to launchd. Only 222 is SIGTERMed (pre-fix: both).
        """
        mock_enum.return_value = [
            (111, _runner_cmdline("AAAAAAAA-1111-1111-1111-111111111111")),
            (222, _runner_cmdline(_ORPHAN_UDID, "tmpstalexx")),
        ]
        mock_parents.return_value = {111: os.getpid(), 222: 1}

        WDAManager._cleanup_orphans()

        mock_parents.assert_called_once_with([111, 222])
        # os.kill is patched module-wide, so pid_alive's signal-0 probe of
        # 111's parent lands here too (and reports it alive). Only SIGTERMs
        # are kills.
        sigterms = [c.args for c in mock_kill.call_args_list if c.args[1] != 0]
        assert sigterms == [(222, wda_mod.signal.SIGTERM)]


class TestTerminatePids:
    @patch("linc_nerve.core.wda.os.kill")
    def test_sigterms_each_pid_and_reports_delivery(self, mock_kill: MagicMock) -> None:
        assert wda_mod._terminate_pids([222, 333]) is True
        assert [c.args for c in mock_kill.call_args_list] == [
            (222, wda_mod.signal.SIGTERM),
            (333, wda_mod.signal.SIGTERM),
        ]

    @patch("linc_nerve.core.wda.os.kill", side_effect=ProcessLookupError)
    def test_already_gone_pids_are_skipped_and_report_no_delivery(
        self, mock_kill: MagicMock
    ) -> None:
        assert wda_mod._terminate_pids([222, 333]) is False
        assert mock_kill.call_count == 2

    @patch("linc_nerve.core.wda.os.kill", side_effect=[ProcessLookupError, None])
    def test_partial_delivery_reports_true(self, mock_kill: MagicMock) -> None:
        assert wda_mod._terminate_pids([222, 333]) is True
        assert mock_kill.call_count == 2

    @patch("linc_nerve.core.wda.os.kill")
    def test_no_pids_signals_nothing(self, mock_kill: MagicMock) -> None:
        assert wda_mod._terminate_pids([]) is False
        mock_kill.assert_not_called()


class TestEnumerateWdaRunners:
    @patch("linc_nerve.core.wda.subprocess.run")
    def test_uses_pgrep_long_full_listing_with_the_shared_pattern(
        self, mock_run: MagicMock
    ) -> None:
        live = _runner_cmdline(_LIVE_UDID)
        mock_run.return_value = MagicMock(returncode=0, stdout=f"111 {live}\n")
        assert wda_mod._enumerate_wda_runners() == [(111, live)]
        mock_run.assert_called_once_with(
            ["pgrep", "-lf", wda_mod._wda_process_pattern()],
            capture_output=True,
            timeout=5,
            text=True,
        )

    @pytest.mark.parametrize("returncode", [1, 2])
    @patch("linc_nerve.core.wda.subprocess.run")
    def test_nonzero_exit_yields_no_candidates(self, mock_run: MagicMock, returncode: int) -> None:
        mock_run.return_value = MagicMock(
            returncode=returncode, stdout=f"111 {_runner_cmdline(_ORPHAN_UDID)}\n"
        )
        assert wda_mod._enumerate_wda_runners() == []

    @pytest.mark.parametrize(
        "exc",
        [wda_mod.subprocess.TimeoutExpired("pgrep", 5), FileNotFoundError("pgrep")],
    )
    @patch("linc_nerve.core.wda.subprocess.run")
    def test_pgrep_hung_or_missing_yields_no_candidates(
        self, mock_run: MagicMock, exc: Exception
    ) -> None:
        mock_run.side_effect = exc
        assert wda_mod._enumerate_wda_runners() == []


# ---------------------------------------------------------------------------
# build_wda
# ---------------------------------------------------------------------------


class TestBuildWda:
    """Characterization tests for build_wda — all branches, fully mocked."""

    @patch("linc_nerve.core.wda._find_xctestrun")
    def test_cache_hit_returns_cached(self, mock_find: MagicMock) -> None:
        """When a cached .xctestrun exists, return it immediately (no build)."""
        cached = Path("/fake/build/Products/test.xctestrun")
        mock_find.return_value = cached

        result = build_wda(for_device=False)

        assert result == cached
        mock_find.assert_called_once()

    @patch("linc_nerve.core.wda._find_xctestrun", return_value=None)
    @patch.dict("os.environ", {}, clear=True)
    def test_missing_wda_project_dir_raises(self, _find: MagicMock) -> None:
        """Missing WDA_PROJECT_DIR env var raises RuntimeError."""
        with pytest.raises(RuntimeError, match="WDA_PROJECT_DIR not set"):
            build_wda()

    @patch("linc_nerve.core.wda._find_xctestrun", return_value=None)
    @patch.dict("os.environ", {"WDA_PROJECT_DIR": "/tmp/fake-wda-src"}, clear=True)
    @patch("linc_nerve.core.wda.Path.exists", return_value=False)
    def test_missing_xcodeproj_raises(self, _exists: MagicMock, _find: MagicMock) -> None:
        """Missing WebDriverAgent.xcodeproj raises RuntimeError."""
        with pytest.raises(RuntimeError, match=r"WebDriverAgent\.xcodeproj not found"):
            build_wda()

    @patch("linc_nerve.core.wda._find_xctestrun")
    @patch("linc_nerve.core.wda.subprocess.run")
    @patch.dict(
        "os.environ",
        {"WDA_PROJECT_DIR": "/tmp/fake-wda-src"},
        clear=True,
    )
    def test_simulator_build_success(
        self, mock_run: MagicMock, mock_find: MagicMock, tmp_path: Path
    ) -> None:
        """Simulator build: xcodebuild succeeds, returns xctestrun path."""
        xctestrun_path = tmp_path / "test.xctestrun"
        # First call: no cache; second call (post-build): found
        mock_find.side_effect = [None, xctestrun_path]
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        with (
            patch("linc_nerve.core.wda.Path.exists", return_value=True),
            patch("linc_nerve.core.wda.Path.mkdir"),
        ):
            result = build_wda(for_device=False)

        assert result == xctestrun_path
        # Verify xcodebuild was called with the generic simulator destination —
        # not a simulator literally named "iPhone" (reactor 1b6fd89f).
        cmd = mock_run.call_args[0][0]
        assert "xcodebuild" in cmd
        assert cmd[cmd.index("-destination") + 1] == "generic/platform=iOS Simulator"
        assert not any("name=iPhone" in arg for arg in cmd)
        # Simulator build is a pure build step — no simctl lookup, just xcodebuild.
        mock_run.assert_called_once()

    @patch("linc_nerve.core.wda._find_xctestrun", return_value=None)
    @patch.dict(
        "os.environ",
        {"WDA_PROJECT_DIR": "/tmp/fake-wda-src"},
        clear=True,
    )
    def test_device_build_missing_team_id_raises(self, _find: MagicMock) -> None:
        """Device build without WDA_TEAM_ID raises RuntimeError."""
        with (
            patch("linc_nerve.core.wda.Path.exists", return_value=True),
            pytest.raises(RuntimeError, match="WDA_TEAM_ID not set"),
        ):
            build_wda(for_device=True)

    @patch("linc_nerve.core.wda._find_xctestrun")
    @patch("linc_nerve.core.wda.subprocess.run")
    @patch.dict(
        "os.environ",
        {"WDA_PROJECT_DIR": "/tmp/fake-wda-src", "WDA_TEAM_ID": "ABCDE12345"},
        clear=True,
    )
    def test_device_build_success(
        self, mock_run: MagicMock, mock_find: MagicMock, tmp_path: Path
    ) -> None:
        """Device build: xcodebuild succeeds with code signing flags."""
        xctestrun_path = tmp_path / "test.xctestrun"
        mock_find.side_effect = [None, xctestrun_path]
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        with (
            patch("linc_nerve.core.wda.Path.exists", return_value=True),
            patch("linc_nerve.core.wda.Path.mkdir"),
        ):
            result = build_wda(for_device=True)

        assert result == xctestrun_path
        cmd = mock_run.call_args[0][0]
        assert "DEVELOPMENT_TEAM=ABCDE12345" in cmd
        assert "generic/platform=iOS" in " ".join(cmd)

    @patch("linc_nerve.core.wda._find_xctestrun", return_value=None)
    @patch("linc_nerve.core.wda.subprocess.run")
    @patch.dict(
        "os.environ",
        {"WDA_PROJECT_DIR": "/tmp/fake-wda-src"},
        clear=True,
    )
    def test_build_failure_raises_with_stderr(self, mock_run: MagicMock, _find: MagicMock) -> None:
        """Build failure (non-zero returncode) raises RuntimeError with stderr."""
        mock_run.return_value = MagicMock(
            returncode=65, stderr="xcodebuild: error: something broke"
        )

        with (
            patch("linc_nerve.core.wda.Path.exists", return_value=True),
            patch("linc_nerve.core.wda.Path.mkdir"),
            pytest.raises(RuntimeError, match="WDA build failed") as exc_info,
        ):
            build_wda(for_device=False)

        assert "something broke" in str(exc_info.value)

    @patch("linc_nerve.core.wda._find_xctestrun", return_value=None)
    @patch("linc_nerve.core.wda.subprocess.run")
    @patch.dict(
        "os.environ",
        {"WDA_PROJECT_DIR": "/tmp/fake-wda-src", "WDA_TEAM_ID": "ABCDE12345"},
        clear=True,
    )
    def test_device_build_failure_includes_device_hints(
        self, mock_run: MagicMock, _find: MagicMock
    ) -> None:
        """Device build failure includes device-specific troubleshooting hints."""
        mock_run.return_value = MagicMock(returncode=65, stderr="signing error")

        with (
            patch("linc_nerve.core.wda.Path.exists", return_value=True),
            patch("linc_nerve.core.wda.Path.mkdir"),
            pytest.raises(RuntimeError) as exc_info,
        ):
            build_wda(for_device=True)

        error_msg = str(exc_info.value)
        assert "WDA build failed" in error_msg
        assert "Device UDID is registered" in error_msg
        assert "WDA_TEAM_ID is correct" in error_msg

    @patch("linc_nerve.core.wda._find_xctestrun")
    @patch("linc_nerve.core.wda.subprocess.run")
    @patch.dict(
        "os.environ",
        {"WDA_PROJECT_DIR": "/tmp/fake-wda-src"},
        clear=True,
    )
    def test_build_success_but_no_xctestrun_raises(
        self, mock_run: MagicMock, mock_find: MagicMock
    ) -> None:
        """Build succeeds (returncode 0) but no .xctestrun found afterward."""
        # First call: no cache; second call (post-build): still not found
        mock_find.side_effect = [None, None]
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        with (
            patch("linc_nerve.core.wda.Path.exists", return_value=True),
            patch("linc_nerve.core.wda.Path.mkdir"),
            pytest.raises(
                RuntimeError,
                match=r"WDA build succeeded but \.xctestrun not found",
            ),
        ):
            build_wda(for_device=False)

    @patch("linc_nerve.core.wda._find_xctestrun", return_value=None)
    @patch("linc_nerve.core.wda.subprocess.run")
    @patch.dict(
        "os.environ",
        {"WDA_PROJECT_DIR": "/tmp/fake-wda-src"},
        clear=True,
    )
    def test_build_failure_empty_stderr(self, mock_run: MagicMock, _find: MagicMock) -> None:
        """Build failure with empty stderr shows '(no stderr)' placeholder."""
        mock_run.return_value = MagicMock(returncode=1, stderr="")

        with (
            patch("linc_nerve.core.wda.Path.exists", return_value=True),
            patch("linc_nerve.core.wda.Path.mkdir"),
            pytest.raises(RuntimeError) as exc_info,
        ):
            build_wda(for_device=False)

        assert "(no stderr)" in str(exc_info.value)


# ---------------------------------------------------------------------------
# WDASession.start
# ---------------------------------------------------------------------------


class TestWDASessionStart:
    """Characterization tests for WDASession.start — all branches, fully mocked."""

    def test_already_running_noop(self) -> None:
        """If WDA is already running, start() returns immediately."""
        session = WDASession("UUID-1234", 8100)
        # Simulate a running process
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running
        session._process = mock_proc

        with patch("linc_nerve.core.wda.build_wda") as mock_build:
            session.start()
            mock_build.assert_not_called()

    @patch("linc_nerve.core.wda.build_wda")
    @patch("linc_nerve.core.wda._prepare_xctestrun")
    @patch("linc_nerve.core.wda.subprocess.Popen")
    @patch("linc_nerve.core.wda.time.monotonic")
    @patch("linc_nerve.core.wda.time.sleep")
    def test_simulator_start_healthy(
        self,
        mock_sleep: MagicMock,
        mock_monotonic: MagicMock,
        mock_popen: MagicMock,
        mock_prepare: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        """Simulator start: build, launch xcodebuild, poll health, succeed."""
        xctestrun = Path("/fake/test.xctestrun")
        mock_build.return_value = xctestrun
        mock_prepare.return_value = Path("/tmp/prepared.xctestrun")

        mock_proc = MagicMock()
        mock_proc.stdout = None
        mock_proc.stderr = None
        mock_popen.return_value = mock_proc

        # Time: first call sets deadline, second call is within deadline,
        # third call would be after but we return healthy first
        mock_monotonic.side_effect = [0.0, 0.0, 1.0]

        session = WDASession("SIMULATOR-UUID-1234", 8100, is_physical=False)

        with (
            patch.object(session, "_is_healthy", return_value=True),
            patch.object(session, "_create_session") as mock_create,
        ):
            session.start()
            mock_create.assert_called_once()

        # Verify simulator destination in xcodebuild command
        popen_cmd = mock_popen.call_args[0][0]
        dest_arg = popen_cmd[popen_cmd.index("-destination") + 1]
        assert "iOS Simulator" in dest_arg
        assert "SIMULATOR-UUID-1234" in dest_arg

        mock_build.assert_called_once_with(for_device=False)

    @patch("linc_nerve.core.wda.build_wda")
    @patch("linc_nerve.core.wda._prepare_xctestrun")
    @patch("linc_nerve.core.wda.subprocess.Popen")
    @patch("linc_nerve.core.wda.time.monotonic")
    @patch("linc_nerve.core.wda.time.sleep")
    def test_physical_start_healthy(
        self,
        mock_sleep: MagicMock,
        mock_monotonic: MagicMock,
        mock_popen: MagicMock,
        mock_prepare: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        """Physical device start: builds for device, uses iOS destination."""
        xctestrun = Path("/fake/test.xctestrun")
        mock_build.return_value = xctestrun
        mock_prepare.return_value = Path("/tmp/prepared.xctestrun")

        mock_proc = MagicMock()
        mock_proc.stdout = None
        mock_proc.stderr = None
        mock_popen.return_value = mock_proc

        mock_monotonic.side_effect = [0.0, 0.0, 1.0]

        session = WDASession("DEVICE-UDID-5678", 8100, is_physical=True)

        with (
            patch.object(session, "_is_healthy", return_value=True),
            patch.object(session, "_create_session") as mock_create,
        ):
            session.start()
            mock_create.assert_called_once()

        # Verify physical device destination
        popen_cmd = mock_popen.call_args[0][0]
        dest_arg = popen_cmd[popen_cmd.index("-destination") + 1]
        assert dest_arg == "platform=iOS,id=DEVICE-UDID-5678"

        # Physical device builds with for_device=True
        mock_build.assert_called_once_with(for_device=True)

    @patch("linc_nerve.core.wda.build_wda")
    @patch("linc_nerve.core.wda._prepare_xctestrun")
    @patch("linc_nerve.core.wda.subprocess.Popen")
    @patch("linc_nerve.core.wda.time.monotonic")
    @patch("linc_nerve.core.wda.time.sleep")
    def test_timeout_raises(
        self,
        mock_sleep: MagicMock,
        mock_monotonic: MagicMock,
        mock_popen: MagicMock,
        mock_prepare: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        """When WDA never becomes healthy, raises RuntimeError after timeout."""
        xctestrun = Path("/fake/test.xctestrun")
        mock_build.return_value = xctestrun
        mock_prepare.return_value = Path("/tmp/prepared.xctestrun")

        mock_proc = MagicMock()
        mock_proc.stdout = None
        mock_proc.stderr = None
        mock_popen.return_value = mock_proc

        # Simulate time passing past the deadline (30s for simulator)
        # monotonic calls: t0=0, deadline check loop: 0, 10, 20, 35 (past deadline)
        mock_monotonic.side_effect = [0.0, 0.0, 10.0, 20.0, 35.0]

        session = WDASession("SIM-UUID-DEAD", 8100, is_physical=False)

        with (
            patch.object(session, "_is_healthy", return_value=False),
            patch.object(session, "stop") as mock_stop,
            pytest.raises(RuntimeError, match="WDA failed to start within"),
        ):
            session.start()
        mock_stop.assert_called_once()

    @patch("linc_nerve.core.wda.build_wda")
    @patch("linc_nerve.core.wda._prepare_xctestrun")
    @patch("linc_nerve.core.wda.subprocess.Popen")
    @patch("linc_nerve.core.wda.time.monotonic")
    @patch("linc_nerve.core.wda.time.sleep")
    def test_physical_timeout_is_doubled(
        self,
        mock_sleep: MagicMock,
        mock_monotonic: MagicMock,
        mock_popen: MagicMock,
        mock_prepare: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        """Physical device gets 2x timeout; error message says 'device' not 'simulator'."""
        xctestrun = Path("/fake/test.xctestrun")
        mock_build.return_value = xctestrun
        mock_prepare.return_value = Path("/tmp/prepared.xctestrun")

        mock_proc = MagicMock()
        mock_proc.stdout = None
        mock_proc.stderr = None
        mock_popen.return_value = mock_proc

        # Physical timeout = 60s (2x 30s). Time passes: 0, 0, 30, 50, 65 (past 60)
        mock_monotonic.side_effect = [0.0, 0.0, 30.0, 50.0, 65.0]

        session = WDASession("DEVICE-UUID", 8100, is_physical=True)

        with (
            patch.object(session, "_is_healthy", return_value=False),
            patch.object(session, "stop"),
            pytest.raises(RuntimeError) as exc_info,
        ):
            session.start()

        error_msg = str(exc_info.value)
        assert "60" in error_msg  # 2x timeout
        assert "device" in error_msg  # not "simulator"
