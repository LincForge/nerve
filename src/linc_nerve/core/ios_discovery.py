"""iOS device discovery — simulators (simctl), physical (devicectl, xctrace).

Split out of ``ios.py`` (#53) as a mixin to keep both files under the hot-file
LOC ceiling. ``logger`` is referenced through the parent module (``_ios.logger``)
so ``patch("linc_nerve.core.ios.logger")`` keeps working; ``subprocess`` is used
directly because the suite patches the global ``subprocess.run``, not
``linc_nerve.core.ios.subprocess``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile

from . import ios as _ios
from .device import DeviceType, NerveDevice, Platform

# devicectl tunnelState values that mean "reachable" — devicectl establishes the
# tunnel on demand at install/launch, so "disconnected" is online. Allowlist, not
# blocklist: unknown future states must default to offline.
_ONLINE_TUNNEL_STATES = frozenset({"connected", "disconnected"})
_TUNNEL_RANK = {"connected": 2, "disconnected": 1}


def _is_devicectl_simulator(dev: dict) -> bool:
    """True for a devicectl entry that is a simulator, not physical hardware.

    Blocklist: an entry with no ``reality`` and a "default" visibilityClass
    (e.g. an unavailable physical iPad) is NOT a simulator.
    """
    return (
        dev.get("visibilityClass") == "simulators"
        or (dev.get("hardwareProperties") or {}).get("reality") == "simulated"
    )


class _DiscoveryMixin:
    """Discovery methods for IOSController (simulators + physical devices)."""

    def discover(self) -> list[NerveDevice]:
        """Discover iOS simulators (simctl) and physical devices (devicectl, xctrace fallback)."""
        devices: list[NerveDevice] = []
        devices.extend(self._discover_simulators())
        devices.extend(self._discover_physical())
        return devices

    def _discover_simulators(self) -> list[NerveDevice]:
        """Discover simulators via simctl."""
        devices: list[NerveDevice] = []
        try:
            result = self._run("list", "devices", "available", "-j")
            data = json.loads(result.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as exc:
            _ios.logger.debug("ios_discovery_failed", error=str(exc))
            return devices

        for runtime, device_list in data.get("devices", {}).items():
            # runtime looks like "com.apple.CoreSimulator.SimRuntime.iOS-17-5"
            runtime_suffix = runtime.rsplit(".", 1)[-1]  # "iOS-17-5"
            # Strip platform prefix (iOS-, watchOS-, tvOS-, etc.)
            parts = runtime_suffix.split("-", 1)
            os_version = parts[-1].replace("-", ".") if len(parts) > 1 else parts[0]

            for sim in device_list:
                state = "online" if sim.get("state") == "Booted" else "offline"
                name = sim.get("name", "")
                udid = sim.get("udid", "")

                if not udid:
                    continue

                self._simulator_serials.add(udid)

                # Infer device type from simulator name
                if "Watch" in name:
                    dt = DeviceType.WATCH
                elif "iPad" in name:
                    dt = DeviceType.TABLET
                else:
                    dt = DeviceType.PHONE

                devices.append(
                    NerveDevice(
                        serial=udid,
                        platform=Platform.IOS,
                        device_type=dt,
                        model=name,
                        os_version=os_version,
                        state=state,
                    )
                )

        return devices

    @staticmethod
    def _device_type_from_devicectl(raw: str) -> DeviceType | None:
        """Map devicectl hardwareProperties.deviceType to DeviceType.

        Substring match, case-insensitive — Apple has shipped both plain
        ("appleWatch") and dotted ("com.apple.watch...") forms. Unknown
        types (appleTV, visionPro, ...) return None and are skipped.
        """
        low = raw.lower()
        if "watch" in low:
            return DeviceType.WATCH
        if "ipad" in low:
            return DeviceType.TABLET
        if "iphone" in low:
            return DeviceType.PHONE
        return None

    def _discover_physical_devicectl(self) -> list[NerveDevice]:
        """Discover physical devices via devicectl (Xcode 15+).

        JSON field mapping verified against Xcode 26.x devicectl output,
        2026-06-09, and re-verified against Xcode 27.0 devicectl output on
        2026-09-28: simulator entries (visibilityClass "simulators" or
        reality "simulated") are skipped. Raises on failure so _discover_physical can fall back
        to the legacy xctrace parser.
        """
        fd, tmp_json = tempfile.mkstemp(suffix=".json", prefix="nerve-devicectl-")
        os.close(fd)
        try:
            subprocess.run(
                ["xcrun", "devicectl", "list", "devices", "--json-output", tmp_json, "-q"],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            with open(tmp_json) as f:
                data = json.load(f)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_json)

        by_udid: dict[str, NerveDevice] = {}
        best_rank: dict[str, int] = {}
        for dev in data.get("result", {}).get("devices", []):
            hw = dev.get("hardwareProperties", {}) or {}
            if _is_devicectl_simulator(dev):
                _ios.logger.debug("devicectl_simulator_skipped", udid=hw.get("udid", ""))
                continue
            conn = dev.get("connectionProperties", {}) or {}
            props = dev.get("deviceProperties", {}) or {}

            udid = hw.get("udid", "")
            if not udid:
                continue

            dt = self._device_type_from_devicectl(hw.get("deviceType", ""))
            if dt is None:
                _ios.logger.info(
                    "devicectl_unknown_device_type",
                    device_type=hw.get("deviceType"),
                    udid=udid,
                )
                continue

            tunnel = conn.get("tunnelState", "")
            rank = _TUNNEL_RANK.get(tunnel, 0)
            if udid in by_udid and rank <= best_rank.get(udid, -1):
                continue  # duplicate (USB + network); keep the better tunnel state

            paired = conn.get("pairingState", "") == "paired"
            state = "online" if paired and tunnel in _ONLINE_TUNNEL_STATES else "offline"

            best_rank[udid] = rank
            self._physical_serials.add(udid)
            by_udid[udid] = NerveDevice(
                serial=udid,
                platform=Platform.IOS,
                device_type=dt,
                model=props.get("name", ""),
                os_version=str(props.get("osVersionNumber") or ""),
                state=state,
            )
        return list(by_udid.values())

    def _discover_physical(self) -> list[NerveDevice]:
        """Discover physical devices: devicectl first, xctrace fallback.

        Malformed JSON warrants a WARNING (devicectl ran but produced
        garbage — schema/version break); missing binary / non-zero exit /
        timeout are expected on older toolchains and stay at debug.
        """
        try:
            return self._discover_physical_devicectl()
        except json.JSONDecodeError as exc:
            _ios.logger.warning("devicectl_malformed_json", error=str(exc))
        except (
            subprocess.CalledProcessError,
            FileNotFoundError,
            subprocess.TimeoutExpired,
        ) as exc:
            _ios.logger.debug("devicectl_discovery_failed", error=str(exc))
        return self._discover_physical_xctrace()

    def _discover_physical_xctrace(self) -> list[NerveDevice]:
        """Discover physical iOS/watchOS devices via xcrun xctrace list devices.

        Parses lines like:
            Test iPhone (18.7.2) (00008110-0000000000000001)
            Test Apple Watch (26.2.1) (00008310-0000000000000002)
        Skips simulators (handled by _discover_simulators) and the Mac itself.
        """
        devices: list[NerveDevice] = []
        try:
            result = subprocess.run(
                ["xcrun", "xctrace", "list", "devices"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except (
            subprocess.CalledProcessError,
            FileNotFoundError,
            subprocess.TimeoutExpired,
        ) as exc:
            _ios.logger.debug("xctrace_discovery_failed", error=str(exc))
            return devices

        # Parse output — physical devices appear between "== Devices ==" and next "==" section
        in_devices = False
        in_offline = False
        # Pattern: "Name (version) (UDID)"
        device_re = re.compile(r"^(.+?)\s+\((\d[\d.]*)\)\s+\(([0-9A-Fa-f-]+)\)\s*$")

        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped == "== Devices ==":
                in_devices = True
                in_offline = False
                continue
            if stripped == "== Devices Offline ==":
                in_devices = True
                in_offline = True
                continue
            if stripped.startswith("== "):
                in_devices = False
                in_offline = False
                continue
            if not in_devices or not stripped:
                continue

            match = device_re.match(stripped)
            if not match:
                continue

            name, version, udid = match.group(1), match.group(2), match.group(3)

            # Skip the Mac itself (typically "Display's MacBook")
            if "MacBook" in name or "Mac" in name:
                continue

            # Infer device type
            if "Watch" in name:
                dt = DeviceType.WATCH
            elif "iPad" in name:
                dt = DeviceType.TABLET
            else:
                dt = DeviceType.PHONE

            state = "offline" if in_offline else "online"

            self._physical_serials.add(udid)
            devices.append(
                NerveDevice(
                    serial=udid,
                    platform=Platform.IOS,
                    device_type=dt,
                    model=name,
                    os_version=version,
                    state=state,
                )
            )

        return devices
