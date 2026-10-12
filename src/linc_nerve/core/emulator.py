"""Emulator lifecycle manager — start, stop, and list Android emulators."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

import structlog

from .adb_path import ADB_PATH, describe_emulator_probe_locations, resolve_emulator_path

logger = structlog.get_logger().bind(domain="device_control")


def _resolve_emulator_or_raise() -> str:
    """Resolve the emulator binary, raising ``RuntimeError`` (naming the probed
    locations) if none can be found anywhere.

    Extracted from ``EmulatorManager._launch_emulator`` so that call site stays
    at its pre-fix complexity — this helper is the single place that turns an
    unresolved ``resolve_emulator_path()`` into a loud, actionable error instead
    of an opaque ``FileNotFoundError`` from deep inside ``subprocess.Popen``.
    """
    emulator_path = resolve_emulator_path()
    if emulator_path is None:
        probed = ", ".join(describe_emulator_probe_locations())
        msg = f"emulator binary not found; cannot start emulator. Probed: {probed}"
        raise RuntimeError(msg)
    return emulator_path


# Port range for emulator allocation (even numbers only)
_PORT_MIN = 5554
_PORT_MAX = 5600


@dataclass
class EmulatorInfo:
    """Information about an Android emulator (AVD)."""

    avd_name: str
    serial: str | None = None  # e.g. "emulator-5554", None if not running
    port: int | None = None  # console port (even number)
    state: str = "stopped"  # "stopped" | "booting" | "running"
    api_level: str = ""  # from AVD config if available
    started_at: float | None = None

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        d: dict = {
            "avd_name": self.avd_name,
            "state": self.state,
        }
        if self.serial is not None:
            d["serial"] = self.serial
        if self.port is not None:
            d["port"] = self.port
        if self.api_level:
            d["api_level"] = self.api_level
        if self.started_at is not None:
            d["started_at"] = self.started_at
        return d


class EmulatorManager:
    """Manage Android emulator lifecycle via the ``emulator`` CLI and ADB."""

    def __init__(self) -> None:
        self._pids: dict[str, int] = {}  # serial -> emulator process PID

    def _run(
        self,
        cmd: list[str],
        timeout: int = 15,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run a subprocess with timeout and return result."""
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    def _allocated_ports(self) -> set[int]:
        """Return set of emulator ports currently in use (from ``adb devices``)."""
        try:
            result = self._run([ADB_PATH, "devices"], check=False)
            ports: set[int] = set()
            for line in result.stdout.strip().splitlines()[1:]:
                serial = line.split()[0] if line.strip() else ""
                if serial.startswith("emulator-"):
                    try:
                        ports.add(int(serial.split("-")[1]))
                    except (IndexError, ValueError):
                        continue
            return ports
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.warning("adb_devices_failed", error=str(exc))
            return set()

    def _get_avd_for_serial(self, serial: str) -> str:
        """Get the AVD name for a running emulator serial."""
        for prop in ("ro.boot.qemu.avd_name", "ro.kernel.qemu.avd_name"):
            try:
                result = self._run(
                    [ADB_PATH, "-s", serial, "shell", "getprop", prop],
                    timeout=10,
                    check=False,
                )
                name = result.stdout.strip()
                if name:
                    return name
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue
        return ""

    def _get_installed_avds(self) -> list[str]:
        emulator_path = resolve_emulator_path()
        if emulator_path is None:
            # No usable binary anywhere (see adb_path.resolve_emulator_path) — this
            # is the launchd-PATH bug (reactor 8d140900): the daemon env has no
            # ANDROID_SDK_ROOT/ANDROID_HOME and a bare "emulator" exec is a
            # FileNotFoundError. Surfacing this distinctly (vs. the try/except
            # below, which is for a resolved-but-still-failing binary) is what
            # lets `nerve_emulator_list` report `android_sdk_resolved: false`
            # instead of a silent iOS-only success.
            logger.warning(
                "emulator_binary_unresolved", probed=describe_emulator_probe_locations()
            )
            return []
        try:
            result = self._run([emulator_path, "-list-avds"], timeout=15)
            return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
        ) as exc:
            logger.warning("emulator_list_avds_failed", error=str(exc))
            return []

    def _get_running_emulators(self) -> dict[str, str]:
        running: dict[str, str] = {}  # avd_name -> serial
        try:
            result = self._run([ADB_PATH, "devices"], check=False)
            for line in result.stdout.strip().splitlines()[1:]:
                parts = line.split()
                if not parts:
                    continue
                serial = parts[0]
                if serial.startswith("emulator-"):
                    avd = self._get_avd_for_serial(serial)
                    if avd:
                        running[avd] = serial
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return running

    def list_avds(self) -> list[EmulatorInfo]:
        """List all available AVDs and their running state."""
        avd_names = self._get_installed_avds()
        running = self._get_running_emulators()

        # Build combined list
        infos: list[EmulatorInfo] = []
        seen_avds: set[str] = set()

        for avd_name in avd_names:
            seen_avds.add(avd_name)
            if avd_name in running:
                serial = running[avd_name]
                port = int(serial.split("-")[1]) if "-" in serial else None
                infos.append(
                    EmulatorInfo(
                        avd_name=avd_name,
                        serial=serial,
                        port=port,
                        state="running",
                        started_at=None,  # Unknown for already-running
                    )
                )
            else:
                infos.append(EmulatorInfo(avd_name=avd_name, state="stopped"))

        # Include running emulators whose AVD wasn't in list-avds (edge case)
        for avd_name, serial in running.items():
            if avd_name not in seen_avds:
                port = int(serial.split("-")[1]) if "-" in serial else None
                infos.append(
                    EmulatorInfo(
                        avd_name=avd_name,
                        serial=serial,
                        port=port,
                        state="running",
                    )
                )

        return infos

    def _preflight_checks(self, avd_name: str) -> EmulatorInfo | int:
        """Validate AVD exists and allocate a port.

        Returns:
            EmulatorInfo if already running, or int port number.

        Raises:
            ValueError: If the AVD does not exist.
            RuntimeError: If no free ports are available.
        """
        avds = self.list_avds()
        avd_names = [a.avd_name for a in avds]
        if avd_name not in avd_names:
            msg = f"AVD '{avd_name}' not found. Available: {avd_names}"
            raise ValueError(msg)

        for avd in avds:
            if avd.avd_name == avd_name and avd.state == "running":
                logger.info("emulator_already_running", avd=avd_name, serial=avd.serial)
                return avd

        used_ports = self._allocated_ports()
        for p in range(_PORT_MIN, _PORT_MAX + 1, 2):
            if p not in used_ports:
                return p

        msg = f"No free emulator ports in range {_PORT_MIN}-{_PORT_MAX}"
        raise RuntimeError(msg)

    def _launch_emulator(
        self,
        avd_name: str,
        port: int,
        headless: bool,
        snapshot: str | None,
    ) -> str:
        """Build command, launch emulator process, store PID. Returns serial.

        Raises:
            RuntimeError: If no ``emulator`` binary can be resolved anywhere
                (see ``_resolve_emulator_or_raise`` / ``adb_path.resolve_emulator_path``)
                — better to fail loudly here than let ``subprocess.Popen`` raise
                an opaque ``FileNotFoundError`` for a bare ``"emulator"`` exec.
        """
        emulator_path = _resolve_emulator_or_raise()

        cmd = [
            emulator_path,
            "-avd",
            avd_name,
            "-port",
            str(port),
            "-no-audio",
            "-no-boot-anim",
        ]
        if headless:
            cmd.append("-no-window")
        if snapshot:
            cmd.extend(["-snapshot", snapshot])

        serial = f"emulator-{port}"
        logger.info("emulator_starting", avd=avd_name, serial=serial, port=port)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._pids[serial] = proc.pid
        return serial

    def _wait_for_device(self, serial: str, deadline: float) -> None:
        """Poll adb devices until serial appears. Raises TimeoutError."""
        while time.monotonic() < deadline:
            try:
                result = self._run([ADB_PATH, "devices"], check=False)
                for line in result.stdout.strip().splitlines()[1:]:
                    if line.startswith(serial) and "device" in line:
                        return
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            time.sleep(2)

        msg = f"Emulator {serial} did not appear in adb devices within timeout"
        raise TimeoutError(msg)

    def _wait_for_boot(self, serial: str, deadline: float, start_mono: float) -> None:
        """Poll sys.boot_completed until '1'. Raises TimeoutError."""
        while time.monotonic() < deadline:
            try:
                result = self._run(
                    [ADB_PATH, "-s", serial, "shell", "getprop", "sys.boot_completed"],
                    timeout=10,
                    check=False,
                )
                if result.stdout.strip() == "1":
                    return
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            time.sleep(2)

        msg = f"Emulator {serial} appeared but did not finish booting within timeout"
        raise TimeoutError(msg)

    def start(
        self,
        avd_name: str,
        snapshot: str | None = None,
        headless: bool = False,
        timeout: int = 120,
    ) -> EmulatorInfo:
        """Start an Android emulator and wait for it to boot.

        Args:
            avd_name: AVD name (must exist in ``emulator -list-avds``).
            snapshot: Optional snapshot name for fast boot.
            headless: Run without display window (``-no-window``).
            timeout: Maximum seconds to wait for boot (default 120).

        Returns:
            EmulatorInfo with serial, port, and running state.

        Raises:
            ValueError: If the AVD does not exist.
            TimeoutError: If the emulator does not boot within timeout.
        """
        result = self._preflight_checks(avd_name)
        if isinstance(result, EmulatorInfo):
            return result
        port = result

        start_mono = time.monotonic()
        started_at = time.time()
        serial = self._launch_emulator(avd_name, port, headless, snapshot)
        deadline = start_mono + timeout
        self._wait_for_device(serial, deadline)
        self._wait_for_boot(serial, deadline, start_mono)
        logger.info(
            "emulator_booted",
            avd=avd_name,
            serial=serial,
            elapsed_s=round(time.monotonic() - start_mono, 1),
        )
        return EmulatorInfo(
            avd_name=avd_name,
            serial=serial,
            port=port,
            state="running",
            started_at=started_at,
        )

    def stop(self, serial: str) -> bool:
        """Stop a running emulator.

        Args:
            serial: Emulator serial (e.g. ``emulator-5554``).

        Returns:
            True if the emulator was stopped successfully.
        """
        if not serial.startswith("emulator-"):
            logger.warning("stop_not_emulator", serial=serial)
            return False

        logger.info("emulator_stopping", serial=serial)

        # Send emu kill
        try:
            self._run([ADB_PATH, "-s", serial, "emu", "kill"], timeout=10, check=False)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.warning("emu_kill_failed", serial=serial, error=str(exc))

        # Wait for disappearance
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                result = self._run([ADB_PATH, "devices"], check=False)
                found = False
                for line in result.stdout.strip().splitlines()[1:]:
                    if line.startswith(serial):
                        found = True
                        break
                if not found:
                    logger.info("emulator_stopped", serial=serial)
                    return True
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            time.sleep(1)

        # Try killing by PID as fallback
        pid = self._pids.pop(serial, None) or self._find_emulator_pid(serial)
        if pid is not None:
            logger.info("emulator_force_kill", serial=serial, pid=pid)
            try:
                import os
                import signal

                os.kill(pid, signal.SIGTERM)
                time.sleep(2)
                return True
            except OSError as exc:
                logger.warning("force_kill_failed", serial=serial, error=str(exc))

        logger.warning("emulator_stop_timeout", serial=serial)
        return False

    def _find_emulator_pid(self, serial: str) -> int | None:
        """Find the PID of a running emulator by its console port."""
        try:
            port_str = serial.split("-")[1]
        except (IndexError, ValueError):
            return None

        try:
            result = self._run(
                ["lsof", "-t", "-i", f":{port_str}"],
                timeout=5,
                check=False,
            )
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line.isdigit():
                    return int(line)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        return None
