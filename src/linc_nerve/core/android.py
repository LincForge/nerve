"""Android device controller — persistent ADB connection via adbutils."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import enum
import io
import os
import queue
import re
import shlex
import socket as _socket_mod
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as ET
import structlog

from . import adb_health
from .adb_path import ADB_PATH
from .device import DeviceError, DeviceType, ElementNode, NerveDevice, Platform
from .device_state import (
    Keyguard,
    Wakefulness,
    compute_readiness,
    parse_battery,
    parse_keyguard,
    parse_power,
    parse_screen_size,
    parse_uiautomation_registered,
)
from .metrics import classify_error
from .scrcpy import ScrcpyManager, server_available

logger = structlog.get_logger().bind(domain="device_control")

# Globals the split sibling mixins reach via ``_android.<name>`` and the suite
# patches as ``linc_nerve.core.android.<name>`` (#53); declared so they stay
# importable and linters treat the now core-unreferenced imports as intentional
# (same pattern as ``wda.py``'s ``__all__``).
__all__ = [
    "ADB_PATH",
    "ET",
    "AndroidController",
    "DeviceError",
    "ElementNode",
    "Path",
    "_socket_mod",
    "base64",
    "classify_error",
    "io",
    "os",
    "parse_screen_size",
    "queue",
    "re",
    "server_available",
    "shlex",
    "subprocess",
]

# Minimum screenshot size in bytes to consider valid (API 35 transparency issue)
_MIN_SCREENSHOT_SIZE = 20_000

# Maximum screenshot size in bytes for MCP responses.  Larger images cause
# Claude Code's MCP client to stall after 2-3 sequential tool calls because
# the 3+ MB base64 responses overwhelm the client-side processing pipeline.
# 768KB PNG → ~1 MB base64 → well within safe territory.
_MAX_SCREENSHOT_BYTES = 768_000

# Retry configuration
_RETRYABLE_ERRORS = (TimeoutError, OSError, ConnectionResetError)
_MAX_RETRIES = 2  # Up to 2 retries (3 total attempts)
_BACKOFF_BASE = 0.5  # 0.5s, 1.0s
_CIRCUIT_FAILURE_THRESHOLD = 3  # Failures before circuit opens
_CIRCUIT_COOLDOWN_SECS = 30  # Hard cooldown duration
_FAILURE_WINDOW_SECS = 10  # Window for distinct-device restart logic
_DISTINCT_DEVICE_RESTART = 3  # Distinct devices needed to trigger restart

# UI hierarchy cache (inbox 23f41765). `uiautomator dump` is ~3.2s p50 on a
# populated Pixel; tight state→elements→state loops were paying it every
# call. 0.5s TTL collapses bursts while invalidations on input mutate the
# cache so post-tap callers see fresh data.
_ELEMENTS_CACHE_TTL = 0.5

# --- Device readiness (ensure_ready) ---
ENSURE_READY_TIMEOUT_S = 15.0  # three settle passes (wakeup/dismiss/swipe) share this deadline
SETTLE_S = 0.5  # kernel transition after WAKEUP/dismiss takes 200-800ms
MAX_SETTLE_POLLS = 4  # re-read up to 4x (~2s) until state settles (Samsung A12 dismiss race)
MAX_RECOVERY_ACTIONS = 3  # WAKEUP + dismiss-keyguard + swipe-dismiss
SWIPE_FROM_FRAC = 0.85  # bouncer swipe start (bottom third, above gesture zone)
SWIPE_TO_FRAC = 0.15  # bouncer swipe end (top third)
SWIPE_MS = 300  # gesture duration: >100ms (not a tap), <500ms (no shade-pull)
SCREEN_OFF_TIMEOUT_MS = 1800000  # 30 min — survive a flakey charge contact mid-suite


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclasses.dataclass
class DeviceCircuitBreaker:
    failure_count: int = 0
    degraded_until: float | None = None  # monotonic timestamp
    state: CircuitState = CircuitState.CLOSED


def _ready_adb_serials(client: Any) -> tuple[str, ...]:
    """Serials in the ``device`` state, read straight off the adb wire protocol.

    ``AdbClient.device_list()`` (via ``AdbClient.list()``) splits ``adb
    devices`` output on ANY whitespace upstream in adbutils — not patchable
    here — which mangles an mDNS wifi-adb serial that itself contains a space
    (``adb-RFAT629KQ9A-x (2)._adb-tls-connect._tcp``). Speaking ``host:devices``
    directly and parsing with ``adb_health.parse_adb_devices`` (which splits on
    the LAST tab) keeps that serial intact.
    """
    with client.make_connection() as conn:
        conn.send_command("host:devices")
        conn.check_okay()
        output = conn.read_string_block()
    return adb_health.parse_adb_devices(output)


# AndroidController is composed from sibling mixin modules (split in #53). The
# mixins reach this module's patched globals via ``_android.<name>`` so the
# suite's ``patch("linc_nerve.core.android.*")`` calls still take effect.
from .android_apps import _AppsMixin  # noqa: E402
from .android_input import _InputMixin  # noqa: E402
from .android_network import _NetworkMixin  # noqa: E402
from .android_reliability import _ReliabilityMixin  # noqa: E402
from .android_screenshot import _ScreenshotMixin  # noqa: E402
from .android_sensor import _SensorRecordMixin  # noqa: E402
from .android_ui import _UiMixin  # noqa: E402


class AndroidController(
    _ScreenshotMixin,
    _ReliabilityMixin,
    _InputMixin,
    _AppsMixin,
    _UiMixin,
    _SensorRecordMixin,
    _NetworkMixin,
):
    """Controls Android devices via adbutils (persistent ADB server socket).

    Falls back to subprocess-based ADB when adbutils is unavailable or for
    operations that require raw command execution.

    Includes ADB client health checking: if the client fails, it is
    recreated on the next call to prevent stale-connection hangs.
    """

    def __init__(self) -> None:
        self._client: Any = None
        self._scrcpy: ScrcpyManager | None = None
        self._scrcpy_available: bool | None = None  # None = unchecked
        self._client_errors: int = 0
        # Per-device circuit breaker state
        self._circuit_breakers: dict[str, DeviceCircuitBreaker] = {}
        # Distinct-device ADB restart tracking (replaces _consecutive_failures)
        self._recent_failures: dict[str, float] = {}  # serial → last failure time (monotonic)
        self._device_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()  # Protects _device_locks dict
        self._metrics: Any = None  # Set externally or lazily via metrics property
        self._recording_processes: dict[str, Any] = {}
        # UI hierarchy cache: (serial, max_depth) → (monotonic_ts, elements)
        self._elements_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}

    def _device_lock(self, serial: str) -> threading.Lock:
        """Get or create a per-device lock to serialize ADB commands."""
        with self._locks_guard:
            if serial not in self._device_locks:
                self._device_locks[serial] = threading.Lock()
            return self._device_locks[serial]

    @contextlib.contextmanager
    def _device_lock_timeout(self, serial: str, timeout: float = 30.0) -> Iterator[None]:
        """Acquire the per-device lock with a timeout.

        Prevents indefinite blocking when an abandoned daemon thread
        holds the lock (e.g., after a _hard_timeout expiry).
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

    # ADB client plumbing (_get_client / _get_device / _reset_client) is inherited
    # from _ReliabilityMixin (#53), alongside the circuit breaker / ADB restart.

    # --- Discovery ---

    def discover(self) -> list[NerveDevice]:
        """Discover all connected Android devices.

        Uses _hard_timeout for shell commands to prevent indefinite hangs
        from stale ADB connections (adbutils timeout= is unreliable).
        """
        devices: list[NerveDevice] = []
        try:
            client = self._get_client()
            for serial in _ready_adb_serials(client):
                d = client.device(serial=serial)
                try:
                    with self._device_lock_timeout(serial, timeout=20):
                        model = self._hard_timeout(
                            d.shell,
                            args=("getprop ro.product.model",),
                            kwargs={"timeout": 10},
                            timeout=12,
                            label=f"discover-model-{serial[:8]}",
                        ).strip()
                        os_ver = self._hard_timeout(
                            d.shell,
                            args=("getprop ro.build.version.release",),
                            kwargs={"timeout": 10},
                            timeout=12,
                            label=f"discover-osver-{serial[:8]}",
                        ).strip()
                        chars = self._hard_timeout(
                            d.shell,
                            args=("getprop ro.build.characteristics",),
                            kwargs={"timeout": 10},
                            timeout=12,
                            label=f"discover-chars-{serial[:8]}",
                        ).strip()

                    if serial.startswith("emulator-"):
                        dt = DeviceType.EMULATOR
                    elif "watch" in chars:
                        dt = DeviceType.WATCH
                    elif "tv" in chars:
                        dt = DeviceType.TV
                    elif "tablet" in chars:
                        dt = DeviceType.TABLET
                    else:
                        dt = DeviceType.PHONE
                except Exception:
                    model = serial
                    os_ver = ""
                    dt = (
                        DeviceType.EMULATOR if serial.startswith("emulator-") else DeviceType.PHONE
                    )

                devices.append(
                    NerveDevice(
                        serial=serial,
                        platform=Platform.ANDROID,
                        device_type=dt,
                        model=model,
                        os_version=os_ver,
                        state="online",
                    )
                )
        except Exception as exc:
            logger.debug("android_discovery_failed", error=str(exc))
        return devices

    def is_ready_serial(self, serial: str) -> bool:
        """True if adb lists *serial* in the ``device`` state right now.

        One ``host:devices`` round trip to the local adb server — no device
        shell and no device lock — so the MCP request path can confirm a
        cached device is still connected without a full ``discover()``.
        Errors propagate; callers fall back to a full rediscovery.
        """
        return serial in _ready_adb_serials(self._get_client())

    # --- Device Readiness ---
    # Screenshot / reliability / input / apps / UI / sensor methods are inherited
    # from the sibling mixin modules (#53). ensure_ready + its only-callers
    # _adb_state/_settle/_screen_size stay here.

    def _adb_state(self, serial: str) -> str:
        """adb get-state: 'device' | 'offline' | 'unauthorized'."""
        try:
            return self._get_device(serial).get_state()
        except Exception:
            return "offline"

    def _settle(self, read: Any, satisfied: Any, deadline: float) -> Any:
        """Poll read() until satisfied(value) or budget exhausted; return the last value.

        Recovery actions (WAKEUP, dismiss-keyguard) settle asynchronously — a single
        immediate re-read catches a mid-transition state (Samsung A12 keyguard dismiss
        observed still-showing 500ms post-dismiss, cleared by ~1s).
        """
        val = read()
        polls = 0
        while not satisfied(val) and polls < MAX_SETTLE_POLLS and time.monotonic() < deadline:
            time.sleep(SETTLE_S)
            val = read()
            polls += 1
        return val

    def _screen_size(self, serial: str) -> tuple[int, int] | None:
        """Current display (w, h) for swipe targeting, or None if unknown.

        Delegates to get_screen_size so the swipe inherits its orientation swap
        (a landscape device reports w>=h — improvement 977b7180); returns None on
        a zero/garbage size so the keyguard swipe fallback skips rather than aiming
        at (0, 0).
        """
        dims = self.get_screen_size(serial)
        w, h = dims["width"], dims["height"]
        return (w, h) if w > 0 and h > 0 else None

    def _uiautomation_gate(self, serial: str, actions: list[str]) -> str | None:
        """Leaked-UiAutomation gate on an otherwise-ready device (44d68b62).

        A UiAutomation registration that outlives its instrumentation (killed
        `am instrument`, leaked driver) hangs every subsequent `am instrument`
        and gets `uiautomator dump` SIGKILLed — BVT callers would discover it
        as an unexplained hang. Detect via the accessibility manager's own
        registration line; force-stop known holder processes (same allowlist
        as the dump-path self-heal, #105) and re-check. Returns None when
        clear (appending a heal action if one was taken), else the not-ready
        reason.
        """
        if not parse_uiautomation_registered(self._shell(serial, "dumpsys accessibility")):
            return None
        stopped = self._kill_uiautomation_holders(serial)
        if stopped and not parse_uiautomation_registered(
            self._shell(serial, "dumpsys accessibility")
        ):
            actions.append("uiautomation_heal:" + ",".join(stopped))
            logger.warning("uiautomation_leak_healed", serial=serial, stopped=stopped)
            return None
        detail = (
            f"force-stopped {', '.join(stopped)} but the registration persists"
            if stopped
            else "no known holder process is alive to force-stop"
        )
        return (
            "uiautomation_leaked — a UiAutomation registration is stuck on the "
            f"device (leaked/active instrumentation); `am instrument` will hang "
            f"and UI dumps get killed. {detail}; reboot the device."
        )

    def ensure_ready(self, serial: str) -> dict[str, Any]:
        """Wake + dismiss keyguard if needed, then report readiness.

        Capped at MAX_RECOVERY_ACTIONS actions and ENSURE_READY_TIMEOUT_S wall-clock.
        Returns {awake, unlocked, battery_pct, charging, ready, reason, actions_taken,
        platform}. Recovery on a not-ready device is a side effect; a properly
        provisioned bench device (lock disabled) reports unlocked after wake alone.
        """
        deadline = time.monotonic() + ENSURE_READY_TIMEOUT_S
        actions: list[str] = []

        state = self._adb_state(serial)
        if state != "device":
            return {
                "awake": False,
                "unlocked": False,
                "battery_pct": None,
                "charging": False,
                "ready": False,
                "reason": "adb_unauthorized",
                "actions_taken": actions,
                "platform": "android",
            }

        is_wear = "watch" in self._shell(serial, "getprop ro.build.characteristics")

        wake = parse_power(self._shell(serial, "dumpsys power"))
        if wake is not Wakefulness.AWAKE and time.monotonic() < deadline:
            self.press_key(serial, "WAKEUP")
            actions.append("wakeup")
            wake = self._settle(
                lambda: parse_power(self._shell(serial, "dumpsys power")),
                lambda w: w is Wakefulness.AWAKE,
                deadline,
            )

        kg = parse_keyguard(self._shell(serial, "dumpsys window"))
        if (
            kg.state is Keyguard.LOCKED
            and not kg.secure
            and not is_wear
            and len(actions) < MAX_RECOVERY_ACTIONS
            and time.monotonic() < deadline
        ):
            self._shell(serial, "wm dismiss-keyguard")
            actions.append("dismiss_keyguard")
            kg = self._settle(
                lambda: parse_keyguard(self._shell(serial, "dumpsys window")),
                lambda k: k.state is Keyguard.UNLOCKED,
                deadline,
            )

        # Some OEM swipe keyguards (Samsung One UI A12) survive `wm dismiss-keyguard`;
        # fall back to a screen-size-derived swipe-up, but ONLY on a confirmed-still-locked
        # re-read so we never swipe an already-dismissed screen.
        if (
            kg.state is Keyguard.LOCKED
            and not kg.secure
            and not is_wear
            and len(actions) < MAX_RECOVERY_ACTIONS
            and time.monotonic() < deadline
        ):
            size = self._screen_size(serial)
            if size is not None:
                w, h = size
                self._shell(
                    serial,
                    f"input touchscreen swipe {w // 2} {int(h * SWIPE_FROM_FRAC)} "
                    f"{w // 2} {int(h * SWIPE_TO_FRAC)} {SWIPE_MS}",
                )
                actions.append("swipe_dismiss")
                kg = self._settle(
                    lambda: parse_keyguard(self._shell(serial, "dumpsys window")),
                    lambda k: k.state is Keyguard.UNLOCKED,
                    deadline,
                )
            # size is None -> can't derive safe coords -> skip swipe, fall through to verdict

        batt = parse_battery(self._shell(serial, "dumpsys battery"))
        ready, reason = compute_readiness(wake, kg, batt.battery_pct, batt.charging)

        if ready:
            leak_reason = self._uiautomation_gate(serial, actions)
            if leak_reason is not None:
                ready, reason = False, leak_reason

        if ready:
            self._shell(serial, f"settings put system screen_off_timeout {SCREEN_OFF_TIMEOUT_MS}")
            actions.append("extend_timeout")
            self._shell(serial, "svc power stayon true")
            actions.append("stayon")
        elif time.monotonic() >= deadline:
            reason = "device_unresponsive"

        return {
            "awake": wake is Wakefulness.AWAKE,
            "unlocked": kg.state is Keyguard.UNLOCKED,
            "battery_pct": batt.battery_pct,
            "charging": batt.charging,
            "ready": ready,
            "reason": reason,
            "actions_taken": actions,
            "platform": "android",
        }
