"""iOS device controller — simulator via simctl, physical via xctrace, WDA for interaction."""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import structlog

from .device import DeviceError
from .observability import linc_home
from .recording import (
    assert_recording_nonempty,
    exited_stderr,
    interrupt_and_reap,
    pop_recording,
    raise_if_exited,
)
from .wda import WDAManager, parse_wda_source
from .wda_mjpeg import WdaMjpegManager

logger = structlog.get_logger().bind(domain="device_control")


def _simulator_state(list_json: dict, udid: str) -> str | None:
    """Walk devices.<runtime>[], return the matching state or None."""
    for devices in list_json.get("devices", {}).values():
        for device in devices:
            if device.get("udid") == udid:
                return device.get("state")
    return None


def _classify_physical(serial: str, physical: set[str], simulators: set[str]) -> bool:
    """Physical-vs-simulator precedence: simulator set, then physical set, then UDID shape."""
    if serial in simulators:
        return False
    if serial in physical:
        return True
    return _is_physical_udid(serial)


def _is_physical_udid(serial: str) -> bool:
    """Check if a serial is a physical device UDID (not a simulator UUID).

    Physical UDIDs: hex strings like '00008110-0000000000000001'
    Simulator UUIDs: standard UUID like '4C215E09-3AAF-498F-B8AF-FE9F8B3B5177'
    """
    # Physical UDIDs are typically 25 chars (8-16 with one dash) or 40 hex chars
    # Simulator UUIDs are 8-4-4-4-12 format (36 chars with dashes)
    return not (len(serial) == 36 and serial.count("-") == 4)


# Concern mixins split out in #53 to keep this file under the hot-file LOC
# ceiling. Imported after ``logger`` / ``_is_physical_udid`` are defined; the
# mixins reach this module's globals (``logger``) via the parent module at
# call-time, so the circular import is safe.
from .ios_apps_logs import _AppsLogsMixin  # noqa: E402
from .ios_discovery import _DiscoveryMixin  # noqa: E402
from .ios_interaction import _InteractionMixin  # noqa: E402
from .ios_logs import _LogsMixin  # noqa: E402


class IOSController(_DiscoveryMixin, _InteractionMixin, _AppsLogsMixin, _LogsMixin):
    """Controls iOS simulators and physical devices.

    Simulators: simctl for most ops, WDA for touch/elements.
    Physical devices: devicectl for install/launch/kill/apps, WDA for touch/elements/screenshot.

    Discovery, screenshot/input, app-lifecycle, and log methods are inherited
    from sibling mixins (ios_discovery / ios_interaction / ios_apps_logs /
    ios_logs, #53).
    """

    def __init__(self) -> None:
        self._wda = WDAManager()
        self._mjpeg = WdaMjpegManager()
        self._physical_serials: set[str] = set()
        self._simulator_serials: set[str] = set()
        self._device_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._recording_processes: dict[str, subprocess.Popen] = {}
        self._recording_paths: dict[str, str] = {}

    def _run(
        self,
        *args: str,
        timeout: int = 30,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Execute a simctl command."""
        cmd = ["xcrun", "simctl", *args]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )

    def _run_binary(
        self,
        *args: str,
        timeout: int = 15,
    ) -> subprocess.CompletedProcess:
        """Execute a simctl command returning binary output."""
        cmd = ["xcrun", "simctl", *args]
        return subprocess.run(
            cmd,
            capture_output=True,
            check=True,
            timeout=timeout,
        )

    def _run_devicectl(
        self,
        *args: str,
        timeout: int = 60,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Execute a devicectl command for physical devices."""
        cmd = ["xcrun", "devicectl", *args]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )

    def _is_physical(self, serial: str) -> bool:
        """Check if serial belongs to a physical device."""
        return _classify_physical(serial, self._physical_serials, self._simulator_serials)

    def _device_lock(self, serial: str) -> threading.Lock:
        """Get or create a per-device lock to serialize commands."""
        with self._locks_guard:
            if serial not in self._device_locks:
                self._device_locks[serial] = threading.Lock()
            return self._device_locks[serial]

    @contextlib.contextmanager
    def _device_lock_timeout(self, serial: str, timeout: float = 30.0) -> Iterator[None]:
        """Acquire the per-device lock with a timeout.

        Prevents indefinite blocking when an abandoned thread
        holds the lock (e.g., after a subprocess timeout expiry).
        """
        lock = self._device_lock(serial)
        acquired = lock.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError(
                f"Could not acquire device lock for {serial} within {timeout}s "
                "(likely held by an abandoned operation)"
            )
        try:
            yield
        finally:
            lock.release()

    # Discovery (_DiscoveryMixin), screenshot+input (_InteractionMixin), and
    # app-lifecycle+logs (_AppsLogsMixin) methods are inherited from sibling
    # modules split out in #53. They reach this module's ``logger`` via the
    # parent module so ``patch("linc_nerve.core.ios.logger")`` still works.

    # --- Simulator Lifecycle ---

    def boot(self, serial: str) -> None:
        """Boot a simulator."""
        self._run("boot", serial, check=False)

    def shutdown(self, serial: str) -> None:
        """Shutdown a simulator."""
        self._run("shutdown", serial, check=False)

    # --- Recording ---

    def _require_recordable(self, serial: str) -> None:
        """Ensure device is a booted simulator before recording."""
        if self._is_physical(serial):
            raise DeviceError(f"iOS recording is simulator-only; {serial} is a physical device")
        data = json.loads(self._run("list", "devices", "-j").stdout)
        state = _simulator_state(data, serial)
        if state != "Booted":
            raise DeviceError(f"simulator {serial} is not booted (state={state})")

    def start_recording(self, serial: str) -> None:
        """Start video recording."""
        self._require_recordable(serial)

        out_dir = linc_home() / "nerve" / "recordings"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{serial}_{int(time.time())}.mp4"

        cmd = [
            "xcrun",
            "simctl",
            "io",
            serial,
            "recordVideo",
            "--codec=h264",
            "--mask=black",
            str(path),
        ]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Give simctl a moment to start recording, then fail closed if the
        # child already died (bad UDID, unsupported display, disk full, ...).
        time.sleep(1.0)
        raise_if_exited(process, "simctl recordVideo")

        self._recording_processes[serial] = process
        self._recording_paths[serial] = str(path)

    def stop_recording(self, serial: str) -> str:
        """Stop a recording by SIGINT-ing simctl and return the verified MP4 path.

        Raises ``ValueError`` when nothing is recording for ``serial`` and
        ``RuntimeError`` when the MP4 is missing or empty.
        """
        process, path = pop_recording(self._recording_processes, self._recording_paths, serial)
        interrupt_and_reap(process)
        assert_recording_nonempty(Path(path), exited_stderr(process))
        return path

    # --- Elements (WDA) ---

    def elements(self, serial: str, max_depth: int = 20) -> list[dict]:
        """Get UI element hierarchy via WDA."""
        with self._device_lock_timeout(serial, timeout=30.0):
            self._require_wda(serial)
            session = self._get_wda(serial)
            tree = session.source()
            # WDA wraps source in {"value": {...}, "sessionId": "..."}
            source = tree.get("value", tree)
            return parse_wda_source(source, max_depth=max_depth)

    # --- Device Info ---

    def get_screen_size(self, serial: str) -> dict[str, int]:
        """Get screen size via WDA if available."""
        session = self._wda._sessions.get(serial)
        if session and session.is_running:
            try:
                return session.screen_size()
            except Exception:
                pass
        return {"width": 0, "height": 0}

    def get_density(self, serial: str) -> dict[str, float]:
        """Get dp→px density factor (best-effort).

        iOS uses points x UIScreen scale, not dpi. We don't introspect the
        scale from WDA today, so return a documented Retina default (2.0).
        No iOS BVT suite uses dp size assertions yet; Android is the live
        consumer of this factor (crucible ``assert_min_size``).
        """
        return {"density_dpi": 0, "density_factor": 2.0, "note": "ios scale not introspected"}

    def get_orientation(self, serial: str) -> str:
        """Get orientation via WDA if available."""
        session = self._wda._sessions.get(serial)
        if session and session.is_running:
            try:
                return session.orientation()
            except Exception:
                pass
        return "portrait"

    # --- Cleanup ---

    def cleanup(self) -> None:
        """Stop all WDA sessions and MJPEG streams."""
        self._wda.stop_all()
        self._mjpeg.stop_all()
