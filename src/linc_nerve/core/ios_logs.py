"""iOS log retrieval (show + stream) for IOSController.

Split out of ``ios.py`` / ``ios_apps_logs.py`` (#53) as a mixin to keep each
module under the hot-file LOC ceiling. ``subprocess`` is used directly (the
suite patches the global ``subprocess.run``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile


class _LogsMixin:
    """Bundle-id resolution + log show/stream methods for IOSController."""

    # --- Logs ---

    def _resolve_bundle_id(self, serial: str, filter_str: str) -> str:
        """If filter looks like a bundle ID (contains dots), resolve to process name.

        Returns the process name (CFBundleExecutable) or raises ValueError
        if the bundle ID isn't found among installed apps.
        """
        if "." not in filter_str:
            return filter_str  # Already a process name

        if self._is_physical(serial):
            return self._resolve_bundle_id_physical(serial, filter_str)

        # Simulator: parse CFBundleIdentifier + CFBundleExecutable from listapps
        result = self._run("listapps", serial, check=False)
        if result.returncode != 0:
            raise ValueError(
                f"Could not list apps to resolve bundle ID '{filter_str}'. "
                "Use the process name instead, or check nerve_apps for installed apps."
            )

        current_bundle: str | None = None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if "CFBundleIdentifier" in stripped and "=" in stripped:
                current_bundle = stripped.split("=", 1)[1].strip().strip('";')
            elif "CFBundleExecutable" in stripped and "=" in stripped:
                executable = stripped.split("=", 1)[1].strip().strip('";')
                if current_bundle == filter_str and executable:
                    return executable

        raise ValueError(
            f"No installed app matches bundle ID '{filter_str}'. "
            "Use the process name instead, or check nerve_apps for installed apps."
        )

    def _resolve_bundle_id_physical(self, serial: str, filter_str: str) -> str:
        """Resolve bundle ID to executable name on a physical device."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            json_path = f.name
        try:
            self._run_devicectl(
                "device",
                "info",
                "apps",
                "--device",
                serial,
                "--json-output",
                json_path,
                check=False,
            )
            with open(json_path) as f:
                data = json.load(f)
            for app in data.get("result", {}).get("apps", []):
                if app.get("bundleIdentifier") == filter_str:
                    exe = app.get("executableName", "")
                    if exe:
                        return exe
                    # Fallback: last component of bundle ID
                    return filter_str.rsplit(".", 1)[-1]
        except (json.JSONDecodeError, FileNotFoundError, KeyError):
            pass
        finally:
            os.unlink(json_path)

        raise ValueError(
            f"No installed app matches bundle ID '{filter_str}'. "
            "Use the process name instead, or check nerve_apps for installed apps."
        )

    def _run_log(
        self,
        *args: str,
        timeout: int = 30,
        check: bool = False,
    ) -> subprocess.CompletedProcess:
        """Execute the macOS log command targeting a physical device."""
        cmd = ["log", *args]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )

    # linc-nerve#128/129 round 1 finding 3: the old `^[\w.\-]+$` allowed a
    # leading `-` (`\w` includes letters/digits/underscore, and `-` is in
    # the class too) — a filter like `-c` matched fine and would have
    # reached `create_subprocess_exec`'s argv as a flag-shaped value.
    # Anchoring the first character to alphanumeric/underscore closes that
    # while still accepting real bundle IDs / process names.
    # Anchored ``\A...\Z`` + ``fullmatch`` (security review 2026-10-02 H1):
    # ``^...$`` with ``re.match`` accepts a trailing newline.
    _FILTER_RE = re.compile(r"\A[A-Za-z0-9_][\w.\-]*\Z")

    def get_logs(
        self,
        serial: str,
        lines: int = 50,
        filter: str = "",
        time_window: str = "5m",
    ) -> str:
        """Get recent log output from an iOS simulator or physical device.

        Args:
            serial: Device UDID.
            lines: Max number of log lines to return.
            filter: Process name or bundle ID to filter logs.
            time_window: Time window for log retrieval (e.g., '5m', '1h', '30s').
        """
        with self._device_lock_timeout(serial, timeout=30.0):
            process_name = ""
            if filter:
                if not self._FILTER_RE.fullmatch(filter):
                    raise ValueError(
                        f"Invalid filter '{filter}'. "
                        "Only alphanumeric characters, dots, hyphens, and underscores are allowed."
                    )
                process_name = self._resolve_bundle_id(serial, filter)

            if self._is_physical(serial):
                return self._get_logs_physical(serial, lines, process_name, time_window)

            # Simulator: xcrun simctl spawn <udid> log show
            cmd_args = [
                "spawn",
                serial,
                "log",
                "show",
                "--last",
                time_window,
                "--style",
                "syslog",
            ]
            if process_name:
                cmd_args.extend(["--predicate", f'process == "{process_name}"'])

            result = self._run(*cmd_args, check=False, timeout=30)
            output = result.stdout or ""
            log_lines = output.strip().splitlines()
            return "\n".join(log_lines[-lines:]) if log_lines else ""

    def _get_logs_physical(
        self,
        serial: str,
        lines: int,
        process_name: str,
        time_window: str,
    ) -> str:
        """Get logs from a physical device via macOS log command."""
        cmd_args = [
            "show",
            "--device",
            serial,
            "--last",
            time_window,
            "--style",
            "syslog",
        ]
        if process_name:
            cmd_args.extend(["--predicate", f'process == "{process_name}"'])

        result = self._run_log(*cmd_args, check=False, timeout=30)
        output = result.stdout or ""
        log_lines = output.strip().splitlines()
        return "\n".join(log_lines[-lines:]) if log_lines else ""

    def get_log_stream_cmd(self, serial: str, filter_str: str = "") -> list[str]:
        """Get the subprocess command list to stream logs continuously."""
        process_name = ""
        if filter_str:
            if not self._FILTER_RE.fullmatch(filter_str):
                raise ValueError(
                    f"Invalid filter '{filter_str}'. "
                    "Only alphanumeric characters, dots, hyphens, and underscores are allowed."
                )
            process_name = self._resolve_bundle_id(serial, filter_str)

        if self._is_physical(serial):
            cmd_args = [
                "log",
                "stream",
                "--device",
                serial,
                "--style",
                "syslog",
            ]
        else:
            cmd_args = [
                "xcrun",
                "simctl",
                "spawn",
                serial,
                "log",
                "stream",
                "--style",
                "syslog",
            ]

        if process_name:
            cmd_args.extend(["--predicate", f'process == "{process_name}"'])
        return cmd_args
