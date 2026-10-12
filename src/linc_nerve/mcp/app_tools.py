"""App lifecycle tools — install, launch, kill, apps, uninstall."""

from __future__ import annotations

import os
from typing import Any

from linc_nerve.mcp import _core
from linc_nerve.mcp._confirm import confirmation_gate
from linc_nerve.mcp._core import device_tool


def _install_path_error(path: str) -> dict[str, str] | None:
    """Validate that path is absolute, returning an error dict if not."""
    if not os.path.isabs(path):
        resolved = os.path.abspath(path)
        return {
            "error": (
                f"Install failed: path must be absolute (got '{path}'; "
                f"resolved against the daemon's cwd it would be '{resolved}'). "
                f'Pass an absolute path, e.g. "$(pwd)/{path}".'
            )
        }
    return None


def _install_failure(path: str, exc: Exception) -> dict[str, str]:
    """Format an install failure, naming a path missing on the daemon host."""
    if not os.path.exists(path):
        return {"error": f"Install failed: {path} does not exist on the daemon host ({exc})"}
    return {"error": f"Install failed: {exc}"}


def _install_checked(platform: str, device: str, path: str) -> dict[str, Any]:
    """Validate path, then install via the platform controller."""
    path_error = _install_path_error(path)
    if path_error:
        return path_error

    ctrl = _core._get_controller(platform)
    try:
        ctrl.install(device, path)
    except Exception as exc:
        return _install_failure(path, exc)
    return {"success": True, "action": "install", "path": path}


@device_tool
def _nerve_install_sync(device: str, path: str) -> dict[str, Any]:
    """Install an app on the device.

    USE FOR: Installing APK (Android) or .app/.ipa (iOS simulator).
    NOT FOR: Building apps (use gradlew or crucible_pour).
    TIP: For Android, pass an APK path. For iOS simulators, pass a .app directory.

    Args:
        device: Device serial.
        path: Path to the APK or .app to install.
    """
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    return _install_checked(platform, device, path)


@device_tool
def _nerve_launch_sync(
    device: str,
    package: str,
    uri: str = "",
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Launch an app by package/bundle ID, optionally opening a deep link URI.

    USE FOR: Starting an app, returning to an app, navigating to a specific screen via deep link,
    priming a consent-gated app for an unattended BVT via intent extras.
    NOT FOR: Installing first (use nerve_install).
    TIP: Use nerve_discover to verify the device, then launch. Android uses
    package names (com.lincmobile.level), iOS uses bundle IDs.

    Args:
        device: Device serial.
        package: Android package name or iOS bundle ID.
        uri: Optional deep link URI (e.g. "precisionlevel://settings"). When provided,
             launches the app directly to the specified screen.
        extras: Optional Android intent extras (e.g. {"SKIP_CONSENT": true}). Values are
             type-dispatched into am-start flags: bool -> --ez, int -> --ei, str -> --es.
             Lets consent-gated apps run unattended BVTs without a manual am-start priming
             step. iOS support is platform-dependent — see core/ios.py launch().
    """
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    ctrl = _core._get_controller(platform)
    try:
        ctrl.launch(device, package, uri=uri, extras=extras)
        if _core._metrics is not None:
            _core._metrics.record_launch(device)
        return _launch_result(package, uri, extras)
    except Exception as exc:
        return {"error": f"Launch failed: {exc}"}


def _launch_result(package: str, uri: str, extras: dict[str, Any] | None) -> dict[str, Any]:
    """Assemble the launch success payload, echoing uri/extras only when set."""
    result: dict[str, Any] = {"success": True, "action": "launch", "package": package}
    if uri:
        result["uri"] = uri
    if extras:
        result["extras"] = extras
    return result


@device_tool
def _nerve_kill_sync(device: str, package: str) -> dict[str, Any]:
    """Force-stop / terminate an app.

    USE FOR: Stopping a running app, clearing app state.
    NOT FOR: Uninstalling (use nerve_uninstall).

    Args:
        device: Device serial.
        package: Android package name or iOS bundle ID.
    """
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    ctrl = _core._get_controller(platform)
    try:
        ctrl.kill(device, package)
        return {"success": True, "action": "kill", "package": package}
    except Exception as exc:
        return {"error": f"Kill failed: {exc}"}


@device_tool
def _nerve_clear_app_data_sync(device: str, package: str, confirm: bool = False) -> dict[str, Any]:
    """Wipe an app's data + cache → first-install state.

    USE FOR: forcing a true cold start so consent/onboarding screens reappear
    in a test (deeper than nerve_kill, which only force-stops).
    NOT FOR: uninstalling (use nerve_uninstall); just stopping (use nerve_kill).
    CAVEAT (Android): ``pm clear`` revokes granted runtime permissions and
    removes AndroidKeystore aliases — re-grant with nerve_grant_permission if
    the next step needs one.
    Android + iOS simulators; iOS is uninstall-based and returns
    ``needs_reinstall: true`` — reinstall the app before the next launch.
    Physical iOS unsupported (no clean wipe primitive — use a simulator).

    Args:
        device: Device serial.
        package: Android package name.
        confirm: Must be exactly ``True`` to proceed — this is a destructive,
            irreversible action. Omitted, ``False``, or any other value
            (including a stringly-typed ``"false"`` from a JSON-RPC caller)
            is rejected with ``{"success": False, "error": "confirmation_required"}``
            without touching the device.
    """
    gate = confirmation_gate(confirm)
    if gate is not None:
        return gate

    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    ctrl = _core._get_controller(platform)
    try:
        needs = ctrl.clear_app_data(device, package)
        return {
            "success": True,
            "action": "clear_app_data",
            "package": package,
            "needs_reinstall": bool(needs),
        }
    except Exception as exc:
        return {"error": f"Clear app data failed: {exc}"}


@device_tool
def _nerve_grant_permission_sync(
    device: str, package: str, permission: str, confirm: bool = False
) -> dict[str, Any]:
    """Grant a runtime permission to an app (Android ``pm grant``) — no dialog.

    USE FOR: re-granting a permission after nerve_clear_app_data wiped it, or
    pre-granting (e.g. android.permission.CAMERA) so a flow skips the system
    consent dialog.
    NOT FOR: app-level consent (DOB/parental gates) — those are UI flows.
    Android-only; returns an error on iOS. Only works for permissions the app
    declares as runtime permissions in its manifest.

    Args:
        device: Device serial.
        package: Android package name.
        permission: Full permission string, e.g. "android.permission.CAMERA".
        confirm: Must be exactly ``True`` to proceed — this is a destructive,
            irreversible action. Omitted, ``False``, or any other value
            (including a stringly-typed ``"false"`` from a JSON-RPC caller)
            is rejected with ``{"success": False, "error": "confirmation_required"}``
            without touching the device.
    """
    gate = confirmation_gate(confirm)
    if gate is not None:
        return gate

    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    ctrl = _core._get_controller(platform)
    try:
        ctrl.grant_permission(device, package, permission)
        return {
            "success": True,
            "action": "grant_permission",
            "package": package,
            "permission": permission,
        }
    except Exception as exc:
        return {"error": f"Grant permission failed: {exc}"}


@device_tool
def _nerve_apps_sync(device: str) -> dict[str, Any]:
    """List installed apps on the device.

    USE FOR: Checking what's installed, finding package names.
    NOT FOR: Detailed app info (use platform-specific tools).

    Args:
        device: Device serial.
    """
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    ctrl = _core._get_controller(platform)
    try:
        apps = ctrl.list_apps(device)
        return {"apps": apps, "count": len(apps)}
    except Exception as exc:
        return {"error": f"List apps failed: {exc}"}


@device_tool
def _nerve_uninstall_sync(device: str, package: str, confirm: bool = False) -> dict[str, Any]:
    """Uninstall an app from the device.

    USE FOR: Removing an app completely.
    NOT FOR: Stopping (use nerve_kill).

    Args:
        device: Device serial.
        package: Android package name or iOS bundle ID.
        confirm: Must be exactly ``True`` to proceed — this is a destructive,
            irreversible action. Omitted, ``False``, or any other value
            (including a stringly-typed ``"false"`` from a JSON-RPC caller)
            is rejected with ``{"success": False, "error": "confirmation_required"}``
            without touching the device.
    """
    gate = confirmation_gate(confirm)
    if gate is not None:
        return gate

    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    ctrl = _core._get_controller(platform)
    try:
        ctrl.uninstall(device, package)
        return {"success": True, "action": "uninstall", "package": package}
    except Exception as exc:
        return {"error": f"Uninstall failed: {exc}"}
