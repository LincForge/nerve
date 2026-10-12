"""iOS app lifecycle for IOSController.

Split out of ``ios.py`` (#53) as a mixin to keep both files under the hot-file
LOC ceiling. Covers install/launch/kill/list/uninstall, the simulator
cold-start reset (``clear_app_data`` — uninstall-based, needs_reinstall
contract), and the Android-only no-op guard (``grant_permission``). Log
retrieval lives in the sibling ``ios_logs`` mixin.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

from . import ios as _ios

# devicectl error 3002 ("could not be located during the last connection") is
# a transient CoreDevice bookmark-resolution failure — a stale device
# reference that a bare retry usually resolves. Matched case-insensitively
# against combined stdout/stderr since the message wording (with or without
# the numeric code) varies across Xcode versions.
_TRANSIENT_DEVICECTL_MARKERS = ("3002", "bookmark")


def _is_transient_devicectl_error(exc: subprocess.CalledProcessError) -> bool:
    """True if a devicectl failure looks like the transient 3002/bookmark error."""
    text = " ".join(filter(None, (str(exc.stdout or ""), str(exc.stderr or "")))).lower()
    return any(marker in text for marker in _TRANSIENT_DEVICECTL_MARKERS)


# ``simctl uninstall`` of an absent app fails with wording that varies across
# Xcode versions ("No such file or directory" vs "... is not installed ...").
# Matched case-insensitively against combined stdout/stderr — used to make
# clear_app_data idempotent (the first-ever cold_start hits this path).
_NOT_INSTALLED_MARKERS = ("no such file or directory", "not installed")


def _is_not_installed_error(exc: subprocess.CalledProcessError) -> bool:
    """True if a simctl uninstall failure means the app was never installed."""
    text = " ".join(filter(None, (str(exc.stdout or ""), str(exc.stderr or "")))).lower()
    return any(marker in text for marker in _NOT_INSTALLED_MARKERS)


class _AppsLogsMixin:
    """App-lifecycle methods for IOSController."""

    # --- App Lifecycle ---

    def install(self, serial: str, app_path: str) -> None:
        """Install an app on a simulator or physical device.

        Physical-device installs get one retry on a transient devicectl 3002
        ("could not be located during the last connection") / bookmark
        resolution error — see ``_is_transient_devicectl_error``. Simulator
        installs and non-transient physical failures are not retried.
        """
        with self._device_lock_timeout(serial, timeout=120.0):
            if self._is_physical(serial):
                self._install_physical_with_retry(serial, app_path)
            else:
                self._run("install", serial, app_path, timeout=120)

    def _install_physical_with_retry(self, serial: str, app_path: str) -> None:
        """Install on a physical device, retrying once on a transient error."""
        try:
            self._run_devicectl(
                "device",
                "install",
                "app",
                "--device",
                serial,
                app_path,
                timeout=120,
            )
        except subprocess.CalledProcessError as exc:
            if not _is_transient_devicectl_error(exc):
                raise
            self._run_devicectl(
                "device",
                "install",
                "app",
                "--device",
                serial,
                app_path,
                timeout=120,
            )

    def _extras_launch_args(
        self, serial: str, uri: str, extras: dict[str, str | bool | int] | None
    ) -> list[str]:
        """Map Android-style intent ``extras`` to simctl launch args, or raise.

        iOS has no ``am start`` analog. ``simctl launch`` forwards trailing
        arguments into the app's ``argv``, and Foundation's NSUserDefaults
        *argument domain* parses leading ``-KEY VALUE`` pairs — so
        ``{"SKIP_CONSENT": True}`` becomes ``-SKIP_CONSENT YES`` and the app
        reads it via ``UserDefaults.standard.bool(forKey:)`` with no priming
        step. Mapping: ``bool`` → ``YES``/``NO``, ``int`` → decimal, ``str`` →
        verbatim; any other type is a validation error.

        This works on the **simulator** only. ``devicectl``'s argument passing
        on physical devices is version-dependent and unreliable, and the
        ``openurl``/WDA deep-link path carries no launch args — so ``extras``
        on either of those paths raises ``NotImplementedError`` rather than
        silently no-op'ing. Returns ``[]`` when no extras are supplied.
        """
        if not extras:
            return []
        if uri:
            raise NotImplementedError(
                "iOS launch: intent extras cannot be combined with a uri/deep-link "
                "launch (openurl/WDA carries no launch arguments)"
            )
        if self._is_physical(serial):
            raise NotImplementedError(
                "iOS launch: intent extras are unsupported on physical devices "
                "(devicectl argument-domain passing is unreliable); use a simulator"
            )
        args: list[str] = []
        for key, value in extras.items():
            if isinstance(value, bool):
                args += [f"-{key}", "YES" if value else "NO"]
            elif isinstance(value, int):
                args += [f"-{key}", str(value)]
            elif isinstance(value, str):
                args += [f"-{key}", value]
            else:
                raise ValueError(
                    f"Unsupported intent extras value type for {key!r}: "
                    f"{type(value).__name__} (expected str, bool, or int)"
                )
        return args

    def launch(
        self,
        serial: str,
        bundle_id: str,
        activity: str = "",
        uri: str = "",
        extras: dict[str, str | bool | int] | None = None,
    ) -> None:
        """Launch an app by bundle ID, optionally to a deep link URI.

        ``extras`` are the cross-platform analog of Android intent extras; on
        iOS they are simulator-only (NSUserDefaults argument domain) — see
        ``_extras_launch_args`` for the mapping and the physical-device /
        deep-link limitations.
        """
        extra_args = self._extras_launch_args(serial, uri, extras)
        with self._device_lock(serial):
            if uri:
                if self._is_physical(serial):
                    session = self._get_wda(serial)
                    session._request("POST", "/url", json={"url": uri})
                else:
                    self._run("openurl", serial, uri)
            elif self._is_physical(serial):
                self._run_devicectl(
                    "device",
                    "process",
                    "launch",
                    "--device",
                    serial,
                    bundle_id,
                )
            else:
                self._run("launch", serial, bundle_id, *extra_args)

    def kill(self, serial: str, bundle_id: str) -> None:
        """Terminate an app."""
        with self._device_lock(serial):
            if self._is_physical(serial):
                self._run_devicectl(
                    "device",
                    "process",
                    "terminate",
                    "--device",
                    serial,
                    bundle_id,
                    check=False,
                )
            else:
                self._run("terminate", serial, bundle_id, check=False)

    def list_apps(self, serial: str) -> list[str]:
        """List installed apps on a simulator or physical device."""
        with self._device_lock_timeout(serial, timeout=30.0):
            if self._is_physical(serial):
                return self._list_apps_physical(serial)
            result = self._run("listapps", serial, check=False)
            if result.returncode != 0:
                return []
            # simctl listapps outputs OpenStep plist format:
            #   CFBundleIdentifier = "com.example.app";
            apps: list[str] = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if "CFBundleIdentifier" in line and "=" in line:
                    # Parse: CFBundleIdentifier = "bundle.id";
                    value = line.split("=", 1)[1].strip().strip('";')
                    if value:
                        apps.append(value)
            return sorted(set(apps))

    def _list_apps_physical(self, serial: str) -> list[str]:
        """List installed apps on a physical device via devicectl."""
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
            # devicectl JSON output has result.apps[].bundleIdentifier
            apps_list = data.get("result", {}).get("apps", [])
            return sorted(
                {
                    app.get("bundleIdentifier", "")
                    for app in apps_list
                    if app.get("bundleIdentifier")
                }
            )
        except (json.JSONDecodeError, FileNotFoundError, KeyError):
            return []
        finally:
            os.unlink(json_path)

    def uninstall(self, serial: str, bundle_id: str) -> None:
        """Uninstall an app from a simulator or physical device."""
        with self._device_lock(serial):
            if self._is_physical(serial):
                self._run_devicectl(
                    "device",
                    "install",
                    "uninstall",
                    "--device",
                    serial,
                    bundle_id,
                )
            else:
                self._run("uninstall", serial, bundle_id)

    def clear_app_data(self, serial: str, bundle_id: str) -> bool:
        """Cold-start reset on a simulator via uninstall → needs_reinstall.

        ``pm clear`` has no iOS analog that wipes data in place while
        preserving the install, so the simulator path uninstalls the app
        (``simctl uninstall``) and returns ``True`` — the needs_reinstall
        contract: the caller must reinstall from its build artifact before
        the next launch. Idempotent: an "app not installed" failure (the
        first-ever cold_start) still returns ``True``; any other failure
        re-raises. Physical devices raise ``NotImplementedError`` — iOS has
        no clean wipe primitive there either, and no scripted reinstall path.
        """
        if self._is_physical(serial):
            raise NotImplementedError(
                "clear_app_data on physical iOS is not supported — physical "
                "iOS has no clean wipe primitive; use a simulator."
            )
        with self._device_lock(serial):
            try:
                self._run("uninstall", serial, bundle_id)
            except subprocess.CalledProcessError as exc:
                if not _is_not_installed_error(exc):
                    raise
                _ios.logger.debug(
                    "clear_app_data_not_installed",
                    serial=serial[:12],
                    bundle_id=bundle_id,
                    stderr=str(exc.stderr or "")[:200],
                )
            return True

    def grant_permission(self, serial: str, bundle_id: str, permission: str) -> None:
        """Not supported on iOS via this contract.

        iOS uses simctl privacy *service* names (e.g. ``camera``), not Android
        permission strings, so a shared signature would leak. Use simctl
        directly if a future iOS cold-start flow needs it.
        """
        raise NotImplementedError(
            "grant_permission is Android-only (pm grant). iOS uses simctl "
            "privacy service names, not android.permission.* strings."
        )
