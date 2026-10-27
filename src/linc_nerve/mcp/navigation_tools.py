"""Deep-link navigation tools: nerve_navigate + nerve_navigate_screenshot.

Split out of ``ui_tools.py`` (Batch 4 N5) to keep both files under the 400-line
advisory. The route registry (N1) is loaded from ``routes.yaml`` — override the
whole file via ``NERVE_ROUTES_FILE`` — with a hardcoded fallback baked in below.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog
import yaml

from linc_nerve.mcp import _core
from linc_nerve.mcp._core import device_tool
from linc_nerve.mcp._stability import (
    _build_nav_screenshot_result,
    _check_pil_support,
    _poll_stability,
    _resolve_screenshot_data,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Route registry — loaded from routes.yaml, env-overridable, hardcoded fallback
# ---------------------------------------------------------------------------

_ROUTES_FILE = Path(__file__).parent / "routes.yaml"

# Hardcoded fallback — mirrors routes.yaml. Used when the yaml (or a
# NERVE_ROUTES_FILE override) is missing or unparseable, so navigation never
# hard-fails on a bad data file.
_FALLBACK_APPS: dict[str, dict[str, Any]] = {
    "precisionlevel": {
        "package": "com.lincmobile.level",
        "routes": {
            "home": "precisionlevel://screen/home",
            "level": "precisionlevel://screen/level",
            "calibration": "precisionlevel://screen/calibration",
            "settings": "precisionlevel://screen/settings",
        },
    },
    "sniperpulse": {
        "package": "com.lincmobile.sniperpulse",
        "routes": {
            "home": "sniperpulse://screen/home",
            "session_detail": "sniperpulse://screen/session_detail",
            "history": "sniperpulse://screen/history",
            "settings": "sniperpulse://screen/settings",
        },
    },
}


def _load_route_registry() -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Load the (routes, packages) registry from yaml, falling back on error.

    Path precedence: ``$NERVE_ROUTES_FILE`` → bundled ``routes.yaml``. Any load
    or parse failure logs a warning and returns the hardcoded ``_FALLBACK_APPS``.
    """
    path = os.getenv("NERVE_ROUTES_FILE") or str(_ROUTES_FILE)
    apps: dict[str, dict[str, Any]]
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        apps = data["apps"]
        if not isinstance(apps, dict) or not apps:
            raise ValueError("routes yaml has no 'apps' mapping")
    except Exception as exc:
        log.warning("route_registry_load_failed", path=path, error=str(exc))
        apps = _FALLBACK_APPS

    registry = {app: dict(cfg["routes"]) for app, cfg in apps.items()}
    packages = {app: cfg["package"] for app, cfg in apps.items() if cfg.get("package")}
    return registry, packages


_ROUTE_REGISTRY, _APP_PACKAGES = _load_route_registry()


@device_tool
def _nerve_navigate_sync(device: str, app_id: str, route_id: str) -> dict[str, Any]:
    """Navigate to a named screen of a REGISTERED app (route registry) by its deep link.

    USE FOR: Going to a specific screen by name (e.g. "settings", "calibration").
    NOT FOR: Opening arbitrary URIs (use nerve_launch with uri param).
    TIP: If the route is unknown, the error lists all available routes.

    Args:
        device: Device serial.
        app_id: App identifier (e.g. "precisionlevel", "sniperpulse").
        route_id: Named route (e.g. "settings", "calibration", "home").
    """
    from linc_nerve.mcp.app_tools import _nerve_launch_sync

    app_routes = _ROUTE_REGISTRY.get(app_id)
    if app_routes is None:
        available_apps = sorted(_ROUTE_REGISTRY)
        return {
            "error": (f"Unknown app '{app_id}'. Available apps: {', '.join(available_apps)}"),
            "validation_error": True,
            "available_apps": available_apps,
            "hint": (
                "nerve_navigate only knows apps listed in its route registry "
                "(routes.yaml / NERVE_ROUTES_FILE). For any other app use "
                "nerve_launch(device, package, uri=<deep link>)."
            ),
        }
    uri = app_routes.get(route_id)
    if uri is None:
        available_routes = sorted(app_routes)
        return {
            "error": f"Unknown route '{route_id}' for app '{app_id}'. "
            f"Available routes: {', '.join(available_routes)}",
            "validation_error": True,
            "available_routes": available_routes,
        }
    package = _APP_PACKAGES.get(app_id, app_id)
    return _nerve_launch_sync(device, package, uri=uri)


@device_tool
def _nerve_navigate_screenshot_sync(
    device: str,
    app_id: str,
    route_id: str,
    settle_ms: int = 500,
    annotate: bool = False,
) -> dict[str, Any]:
    """Navigate to a screen and capture a screenshot after the UI settles.

    USE FOR: Visual verification after navigation — the core BVT pattern.
    NOT FOR: Screenshots without navigation (use nerve_screenshot).
    TIP: Combines nerve_navigate + settle delay + nerve_screenshot in one call.
         Default settle time is 500ms. Increase for slow transitions.
         Set annotate=true to overlay numbered bounding boxes on UI elements.

    Args:
        device: Device serial.
        app_id: App identifier (e.g. "precisionlevel", "sniperpulse").
        route_id: Named route (e.g. "settings", "calibration", "home").
        settle_ms: Milliseconds to wait after navigation before screenshot (default 500).
        annotate: If true, overlay numbered bounding boxes on UI elements.
    """
    from linc_nerve.mcp.screenshot_tools import _nerve_screenshot_sync

    nav_result = _nerve_navigate_sync(device, app_id, route_id)
    if "error" in nav_result:
        return nav_result

    _dev, platform = _core._resolve_device(device)
    ctrl = _core._get_controller(platform)

    use_pil = _check_pil_support(ctrl, device, platform)
    stability = _poll_stability(ctrl, device, settle_ms, use_pil)

    data, elements = _resolve_screenshot_data(stability, annotate, ctrl, device)

    # Paths 1/2 succeeded — write to disk and return.
    if data is not None:
        if _core._metrics is not None:
            _core._metrics.record_screenshot(device)

        path = _core._next_output_path(device, "screenshot")
        with open(path, "wb") as f:
            f.write(data)

        return _build_nav_screenshot_result(
            app_id,
            route_id,
            nav_result,
            path,
            len(data),
            settle_ms,
            stability,
            annotate,
            elements,
        )

    # Path 3: Fallback — re-capture via screenshot tool.
    screenshot_result = _nerve_screenshot_sync(device, annotate=annotate)
    if "error" in screenshot_result:
        return screenshot_result

    return _build_nav_screenshot_result(
        app_id,
        route_id,
        nav_result,
        screenshot_result["screenshot"],
        screenshot_result.get("size_bytes", 0),
        settle_ms,
        stability,
        annotate,
        screenshot_result.get("elements") if annotate else None,
    )
