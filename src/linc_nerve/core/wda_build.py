"""WDA build helpers — compile/cache the WebDriverAgent xctestrun.

Split out of ``wda.py`` (#53) to keep both files under the hot-file LOC ceiling.
``build_wda`` and ``_find_xctestrun`` are re-exported from ``wda`` so existing
import paths and the test patch targets
(``patch("linc_nerve.core.wda.build_wda")``, ``..._find_xctestrun``,
``...subprocess``, ``...Path`` ...) keep working: the functions reference the
``wda`` module-globals through the parent module (``_wda.<name>``), so patches
applied on ``linc_nerve.core.wda`` still take effect.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import wda as _wda

_WDA_DEVICE_BUILD_DIR = Path("~/.linc/wda/build-device").expanduser()


def build_wda(for_device: bool = False) -> Path:
    """Build WDA, caching build artifacts.

    Args:
        for_device: If True, build for physical iOS devices (requires code signing).
                    If False, build for simulators (no signing needed).

    Returns the path to the .xctestrun file.
    Raises RuntimeError with actionable message on failure.
    """
    build_dir = _WDA_DEVICE_BUILD_DIR if for_device else _wda._WDA_BUILD_DIR
    xctestrun = _wda._find_xctestrun(build_dir)
    if xctestrun:
        return xctestrun

    project_dir = os.environ.get("WDA_PROJECT_DIR", "")
    if not project_dir:
        msg = (
            "WDA_PROJECT_DIR not set. To enable iOS tap/swipe/elements:\n"
            "  git clone https://github.com/appium/WebDriverAgent.git ~/.linc/wda/src\n"
            "  export WDA_PROJECT_DIR=~/.linc/wda/src"
        )
        raise RuntimeError(msg)

    project_path = _wda.Path(project_dir).expanduser()
    xcodeproj = project_path / "WebDriverAgent.xcodeproj"
    if not xcodeproj.exists():
        msg = f"WebDriverAgent.xcodeproj not found at {xcodeproj}"
        raise RuntimeError(msg)

    if for_device:
        team_id = os.environ.get("WDA_TEAM_ID", "")
        if not team_id:
            msg = (
                "WDA_TEAM_ID not set. Physical device WDA requires code signing.\n"
                "  export WDA_TEAM_ID=<your Apple Developer Team ID>\n"
                "Find your Team ID at https://developer.apple.com/account"
            )
            raise RuntimeError(msg)

    _wda.logger.info("wda_building", project=str(project_path), for_device=for_device)
    t0 = _wda.time.monotonic()

    build_dir.mkdir(parents=True, exist_ok=True)

    if for_device:
        cmd = [
            "xcodebuild",
            "build-for-testing",
            "-project",
            str(xcodeproj),
            "-scheme",
            "WebDriverAgentRunner",
            "-destination",
            "generic/platform=iOS",
            "-derivedDataPath",
            str(build_dir),
            "-allowProvisioningUpdates",
            f"DEVELOPMENT_TEAM={team_id}",
            "CODE_SIGN_IDENTITY=Apple Development",
            "CODE_SIGNING_ALLOWED=YES",
            "-quiet",
        ]
    else:
        cmd = [
            "xcodebuild",
            "build-for-testing",
            "-project",
            str(xcodeproj),
            "-scheme",
            "WebDriverAgentRunner",
            "-destination",
            "generic/platform=iOS Simulator",
            "-derivedDataPath",
            str(build_dir),
            "-quiet",
        ]

    result = _wda.subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )

    elapsed = _wda.time.monotonic() - t0
    if result.returncode != 0:
        stderr = result.stderr[-500:] if result.stderr else "(no stderr)"
        extra = ""
        if for_device:
            extra = (
                "\nFor physical devices, also check:\n"
                "  - Device UDID is registered in developer portal\n"
                "  - WDA_TEAM_ID is correct\n"
                "  - You have a valid Apple Development certificate"
            )
        msg = (
            f"WDA build failed ({elapsed:.0f}s).\n"
            f"stderr: {stderr}\n"
            "Common fixes:\n"
            "  xcode-select --install\n"
            f"  xcodebuild -version  (requires Xcode 15+){extra}"
        )
        raise RuntimeError(msg)

    _wda.logger.info("wda_build_complete", elapsed=f"{elapsed:.1f}s", for_device=for_device)
    xctestrun = _wda._find_xctestrun(build_dir)
    if not xctestrun:
        msg = "WDA build succeeded but .xctestrun not found in build output"
        raise RuntimeError(msg)
    return xctestrun


def _find_xctestrun(build_dir: Path | None = None) -> Path | None:
    """Find cached .xctestrun file."""
    dir_to_use = build_dir if build_dir is not None else _wda._WDA_BUILD_DIR
    products = dir_to_use / "Build" / "Products"
    if not products.exists():
        return None
    return next(products.glob("*.xctestrun"), None)
