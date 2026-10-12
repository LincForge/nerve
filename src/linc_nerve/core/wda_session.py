"""WDA per-device session: process lifecycle + thin HTTP client.

Split out of ``wda.py`` (#53) to keep both files under the hot-file LOC ceiling.
``WDASession`` is re-exported from ``wda`` so
``from linc_nerve.core.wda import WDASession`` keeps working.

The test suite patches module-globals on the ``wda`` module
(``linc_nerve.core.wda.subprocess``, ``...time``, ``...build_wda``,
``..._prepare_xctestrun`` ...). To keep those patches effective after the move,
session methods reference every ``wda`` module-global through the parent module
(``_wda.<name>``) rather than re-importing the names here. Pure-stdlib names the
suite never patches (``contextlib``, ``threading``) are imported directly.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

import httpx

from . import wda as _wda
from .contracts import require


class WDASession:
    """Manages a WDA process and HTTP client for one device (simulator or physical)."""

    def __init__(self, serial: str, port: int, *, is_physical: bool = False) -> None:
        self.serial = serial
        self.port = port
        self.host = "localhost"
        self.is_physical = is_physical
        self._process: Any = None  # subprocess.Popen | None
        self._client: httpx.Client | None = None
        self._session_id: str | None = None
        self._tmp_xctestrun: Any = None  # Path | None
        self.mjpeg_port: int | None = None  # allocated in start(); None = MJPEG disabled

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        """Start WDA on the device (simulator or physical)."""
        if self.is_running:
            return

        xctestrun = _wda.build_wda(for_device=self.is_physical)

        # Inject USE_PORT (+ MJPEG_SERVER_PORT) into a per-session copy of the plist
        self.mjpeg_port = self._acquire_mjpeg_port()
        self._tmp_xctestrun = _wda._prepare_xctestrun(xctestrun, self.port, self.mjpeg_port)
        xctestrun = self._tmp_xctestrun

        if self.is_physical:
            destination = f"platform=iOS,id={self.serial}"
        else:
            destination = f"platform=iOS Simulator,id={self.serial}"

        _wda.logger.info(
            "wda_starting",
            serial=self.serial[:12],
            port=self.port,
            physical=self.is_physical,
        )
        self._url_event = threading.Event()
        self._process = _wda.subprocess.Popen(
            [
                "xcodebuild",
                "test-without-building",
                "-xctestrun",
                str(xctestrun),
                "-destination",
                destination,
            ],
            stdout=_wda.subprocess.PIPE,
            stderr=_wda.subprocess.PIPE,
        )

        # Start background threads to drain stdout/stderr and discover WDA URL.
        # xcodebuild block-buffers when piped, so select()-based reads miss data.
        self._reader_threads: list[threading.Thread] = []
        for stream in (self._process.stdout, self._process.stderr):
            if stream:
                t = threading.Thread(
                    target=self._drain_stream,
                    args=(stream,),
                    daemon=True,
                )
                t.start()
                self._reader_threads.append(t)

        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=_wda._WDA_REQUEST_TIMEOUT,
        )

        # Physical devices take longer to install WDA + start the XCTest runner
        timeout = _wda._WDA_HEALTH_TIMEOUT * 2 if self.is_physical else _wda._WDA_HEALTH_TIMEOUT

        # Poll for health with exponential backoff.
        # The reader threads will update host/port when they find ServerURLHere.
        deadline = _wda.time.monotonic() + timeout
        interval = 0.5
        while _wda.time.monotonic() < deadline:
            # Check if URL was discovered by reader threads
            if self._url_event.is_set():
                self._url_event.clear()  # Reset for potential future changes
            _wda.time.sleep(interval)
            if self._is_healthy():
                self._create_session()
                self._configure_mjpeg_settings()
                _wda.logger.info(
                    "wda_ready",
                    serial=self.serial[:12],
                    url=self.base_url,
                    physical=self.is_physical,
                )
                return
            interval = min(interval * 1.5, 2.0)

        # Timeout — capture stderr for diagnostics
        self.stop()
        device_type = "device" if self.is_physical else "simulator"
        msg = (
            f"WDA failed to start within {timeout}s on {self.serial[:12]}. "
            f"Check {device_type} is booted/connected and Xcode is installed."
        )
        raise RuntimeError(msg)

    def _drain_stream(self, stream: Any) -> None:
        """Read lines from a stream, looking for the WDA ServerURLHere pattern.

        Runs in a background thread to handle xcodebuild's block buffering.
        """
        try:
            for raw_line in stream:
                text = (
                    raw_line.decode("utf-8", errors="replace")
                    if isinstance(raw_line, bytes)
                    else raw_line
                )
                m = _wda._SERVER_URL_RE.search(text)
                if m:
                    actual_host = m.group(1)
                    actual_port = int(m.group(2))
                    if actual_host != self.host or actual_port != self.port:
                        _wda.logger.info(
                            "wda_url_discovered",
                            host=actual_host,
                            port=actual_port,
                            serial=self.serial[:12],
                        )
                        self.host = actual_host
                        self.port = actual_port
                        # Recreate client with correct URL
                        if self._client:
                            with contextlib.suppress(Exception):
                                self._client.close()
                        self._client = httpx.Client(
                            base_url=self.base_url,
                            timeout=_wda._WDA_REQUEST_TIMEOUT,
                        )
                    self._url_event.set()
        except (ValueError, OSError):
            pass  # Stream closed

    def stop(self) -> None:
        """Stop WDA process and close HTTP client."""
        self._session_id = None

        if self._client:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

        if self._process:
            with contextlib.suppress(OSError):
                self._process.kill()
            with contextlib.suppress(_wda.subprocess.TimeoutExpired):
                self._process.wait(timeout=5)
            self._process = None

        if self._tmp_xctestrun and self._tmp_xctestrun.exists():
            self._tmp_xctestrun.unlink()
            self._tmp_xctestrun = None

        _wda._release_mjpeg_port(self.serial)
        self.mjpeg_port = None

        _wda.logger.debug("wda_stopped", serial=self.serial[:12])

    def _extract_session_id(self, data: dict) -> str | None:
        if "sessionId" in data:
            return str(data["sessionId"])
        if "value" in data and isinstance(data["value"], dict):
            return str(data["value"].get("sessionId", "")) or None
        return None

    def _create_session(self) -> None:
        """Create a WDA session (required for touch/element endpoints)."""
        if not self._client:
            return
        try:
            resp = self._client.post("/session", json={"capabilities": {}}, timeout=5.0)
            self._session_id = self._extract_session_id(resp.json())
            _wda.logger.debug("wda_session_created", session_id=self._session_id)
        except Exception as exc:
            _wda.logger.warning("wda_session_create_failed", error=str(exc))

    def _acquire_mjpeg_port(self) -> int | None:
        """Allocate an MJPEG port, degrading to None on pool exhaustion.

        Pool exhaustion disables MJPEG for this session but must NOT break
        tap/swipe — so we warn and return None rather than raise.
        """
        try:
            return _wda._allocate_mjpeg_port(self.serial)
        except RuntimeError as exc:
            _wda.logger.warning(
                "wda_mjpeg_port_unavailable", serial=self.serial[:12], error=str(exc)
            )
            return None

    def _configure_mjpeg_settings(self) -> None:
        """Push MJPEG framerate/quality/scaling. Best-effort: warn + continue.

        The WDA default framerate is 10fps — without this the fast path runs at
        ~100ms staleness instead of ~16ms. Tries twice before giving up.
        """
        if self.mjpeg_port is None or not self._client or not self._session_id:
            return
        payload = {
            "settings": {
                "mjpegServerFramerate": _wda._MJPEG_FRAMERATE,
                "mjpegServerScreenshotQuality": _wda._MJPEG_QUALITY,
                "mjpegScalingFactor": _wda._MJPEG_SCALING,
            }
        }
        path = f"/session/{self._session_id}/appium/settings"
        for attempt in (1, 2):
            try:
                resp = self._client.post(path, json=payload, timeout=5.0)
                if resp.status_code == 200:
                    return
            except Exception as exc:
                if attempt == 2:
                    _wda.logger.warning(
                        "wda_mjpeg_settings_failed",
                        serial=self.serial[:12],
                        error=str(exc),
                        impact="stream may run at WDA default 10fps (~100ms staleness) "
                        "instead of 60fps (~16ms)",
                    )
        _wda.logger.warning(
            "wda_mjpeg_settings_non_200",
            serial=self.serial[:12],
            impact="stream may run at default 10fps",
        )

    def _is_healthy(self) -> bool:
        """Check if WDA is responding."""
        if not self._client:
            return False
        try:
            resp = self._client.get("/status", timeout=2.0)
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
            return False

    def _session_path(self, path: str) -> str:
        """Prepend /session/{id} to a path if we have a session."""
        if self._session_id:
            return f"/session/{self._session_id}{path}"
        return path

    def _is_stale_session_response(self, resp: httpx.Response, full_path: str) -> bool:
        """Check if WDA response is 404 due to a stale/recycled session."""
        return (
            resp.status_code == 404
            and bool(self._session_id)
            and full_path.startswith(f"/session/{self._session_id}")
        )

    def _retry_with_new_session(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Recreate WDA session and retry the request once."""
        _wda.logger.warning(
            "wda_session_stale",
            serial=self.serial[:12],
            session_id=self._session_id,
            path=path,
        )
        self._create_session()
        full_path = self._session_path(path)
        client = require(self._client, "WDA client")
        return client.request(method, full_path, **kwargs)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """HTTP request with single retry + auto-restart on connection failure."""
        if not self._client:
            self.start()
        client = require(self._client, "WDA client")

        full_path = self._session_path(path)
        try:
            resp = client.request(method, full_path, **kwargs)
            if self._is_stale_session_response(resp, full_path):
                resp = self._retry_with_new_session(method, path, **kwargs)
            return resp
        except (httpx.ConnectError, httpx.ReadError):
            _wda.logger.warning("wda_connection_lost", serial=self.serial[:12], path=path)
            # Restart and retry once
            self.stop()
            self.start()
            client = require(self._client, "WDA client")
            full_path = self._session_path(path)
            return client.request(method, full_path, **kwargs)

    # --- Touch ---

    def tap(self, x: int, y: int) -> None:
        """Tap at coordinates."""
        resp = self._request("POST", "/wda/tap", json={"x": x, "y": y})
        resp.raise_for_status()

    def long_press(self, x: int, y: int, duration_ms: int = 500) -> None:
        """Long press at coordinates."""
        resp = self._request(
            "POST",
            "/wda/touchAndHold",
            json={"x": x, "y": y, "duration": duration_ms / 1000.0},
        )
        resp.raise_for_status()

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 300,
    ) -> None:
        """Swipe from one point to another."""
        resp = self._request(
            "POST",
            "/wda/dragfromtoforduration",
            json={
                "fromX": start_x,
                "fromY": start_y,
                "toX": end_x,
                "toY": end_y,
                "duration": duration_ms / 1000.0,
            },
        )
        resp.raise_for_status()

    def source(self) -> dict:
        """Get the UI source tree as JSON."""
        resp = self._request("GET", "/source", params={"format": "json"})
        resp.raise_for_status()
        return resp.json()

    def screen_size(self) -> dict[str, int]:
        """Get screen dimensions."""
        resp = self._request("GET", "/window/size")
        resp.raise_for_status()
        data = resp.json().get("value", {})
        return {"width": int(data.get("width", 0)), "height": int(data.get("height", 0))}

    def orientation(self) -> str:
        """Get device orientation."""
        resp = self._request("GET", "/orientation")
        resp.raise_for_status()
        return resp.json().get("value", "PORTRAIT").lower()
