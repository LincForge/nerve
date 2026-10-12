"""Pure parsers for Android device readiness — no I/O, fully unit-testable.

Sole owner of the readiness gate policy (compute_readiness, apply_network_requirement).
Parsers are three-valued so a degraded parser (dumpsys format drift) is distinguishable
from a real device state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

BATTERY_FLOOR_PCT = 15  # below this AND not charging => skip (avoid mid-suite death)


class Wakefulness(StrEnum):
    AWAKE = "awake"
    DOZING = "dozing"  # AOD / Dreaming — display partial, not interactive
    ASLEEP = "asleep"
    UNKNOWN = "unknown"  # parse failure — fail toward skip


class Keyguard(StrEnum):
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    UNKNOWN = "unknown"  # parse failure — fail toward skip


@dataclass
class KeyguardInfo:
    state: Keyguard
    secure: bool = False


@dataclass
class BatteryInfo:
    battery_pct: int | None
    charging: bool


_WAKE_MAP = {
    "awake": Wakefulness.AWAKE,
    "napping": Wakefulness.AWAKE,  # Wear OS wrist-tilt interactive
    "dreaming": Wakefulness.DOZING,
    "dozing": Wakefulness.DOZING,
    "asleep": Wakefulness.ASLEEP,
}
_DISPLAY_MAP = {
    "on": Wakefulness.AWAKE,
    "doze": Wakefulness.DOZING,
    "doze_suspend": Wakefulness.DOZING,
    "vr": Wakefulness.AWAKE,
    "off": Wakefulness.ASLEEP,
}


def parse_power(dumpsys_power: str) -> Wakefulness:
    m = re.search(r"mWakefulness=(\w+)", dumpsys_power)
    if m:
        return _WAKE_MAP.get(m.group(1).lower(), Wakefulness.UNKNOWN)
    m = re.search(r"Display Power:\s*state=(\w+)", dumpsys_power)
    if m:
        return _DISPLAY_MAP.get(m.group(1).lower(), Wakefulness.UNKNOWN)
    return Wakefulness.UNKNOWN


def parse_keyguard(dumpsys_window: str) -> KeyguardInfo:
    # AOSP exposes `mKeyguardSecure=true`; Samsung One UI (A12, test bench phone) instead
    # reports a bare `secure=true` line under KeyguardServiceDelegate. Match either, but
    # anchor the bare form to line-start so it can't catch `mSimSecure=` / `mKeyguardSecure=`.
    secure = (
        re.search(r"mKeyguardSecure=true", dumpsys_window) is not None
        or re.search(r"(?im)^\s*secure=true\b", dumpsys_window) is not None
    )
    showing: bool | None = None
    # AOSP: mKeyguardShowing=; Samsung Android 12: isKeyguardShowing= (live: tab-active3).
    m = re.search(r"\b(?:is|m)KeyguardShowing=(true|false)", dumpsys_window)
    if m:
        showing = m.group(1) == "true"
    else:
        m = re.search(r"mDreamingLockscreen=(true|false)", dumpsys_window)
        if m:
            showing = m.group(1) == "true"
    if showing is None:
        return KeyguardInfo(Keyguard.UNKNOWN, secure)
    return KeyguardInfo(Keyguard.LOCKED if showing else Keyguard.UNLOCKED, secure)


def parse_battery(dumpsys_battery: str) -> BatteryInfo:
    level = re.search(r"level:\s*(\d+)", dumpsys_battery)
    scale = re.search(r"scale:\s*(\d+)", dumpsys_battery)
    pct: int | None = None
    if level:
        lv = int(level.group(1))
        sc = int(scale.group(1)) if scale else 100
        pct = round(lv / sc * 100) if sc else lv
    status = re.search(r"status:\s*(\d+)", dumpsys_battery)
    charging = bool(status and status.group(1) in ("2", "5"))
    if re.search(r"(AC|USB|Wireless|Dock) powered:\s*true", dumpsys_battery):
        charging = True
    return BatteryInfo(pct, charging)


def parse_screen_size(wm_size_output: str) -> tuple[int, int] | None:
    """Parse `wm size` into (width, height). Override wins over Physical.

    Returns None on parse failure or degenerate 0-dimensions (a confused
    `wm size reset` can emit `Override size: 0x0`), so callers skip the swipe
    rather than aim at (0, 0).
    """
    dims: dict[str, tuple[int, int]] = {}
    for kind, w, h in re.findall(r"(Physical|Override) size:\s*(\d+)x(\d+)", wm_size_output):
        dims[kind] = (int(w), int(h))
    chosen = dims.get("Override") or dims.get("Physical")
    if chosen is None or chosen[0] <= 0 or chosen[1] <= 0:
        return None
    return chosen


def parse_uiautomation_registered(dumpsys_accessibility: str) -> bool:
    """True when a UiAutomation connection is registered on the device.

    The accessibility manager prints a line-anchored ``Ui Automation[...]``
    entry while any UiAutomation client is connected (live capture, bench
    Pixel 8 Pro 2026-07-11). A registration that outlives its instrumentation
    (killed `am instrument`, leaked driver) hangs every subsequent
    `am instrument` and gets `uiautomator dump` SIGKILLed — 44d68b62 /
    8bba730a family. Line-anchored so prose mentions can't trip it.
    """
    return re.search(r"(?m)^\s*Ui Automation\[", dumpsys_accessibility) is not None


def compute_readiness(
    wakefulness: Wakefulness,
    keyguard: KeyguardInfo,
    battery_pct: int | None,
    charging: bool,
) -> tuple[bool, str]:
    """The gate policy. Called on the FINAL (post-recovery) state."""
    if wakefulness is not Wakefulness.AWAKE:
        label = {Wakefulness.ASLEEP: "asleep", Wakefulness.DOZING: "dozing (AOD)"}.get(
            wakefulness, "unknown wakefulness"
        )
        return False, f"{label} (wake failed)"
    if keyguard.state is Keyguard.UNKNOWN:
        return False, "keyguard_state_indeterminate"
    if keyguard.state is Keyguard.LOCKED:
        if keyguard.secure:
            # One actionable verdict for any secure lock — a PIN/pattern/password or a
            # post-reboot Before-First-Unlock device both need the same operator action
            # (nerve can't dismiss either). The old battery>=95 BFU heuristic misfired on
            # every charging bench device, masking this message.
            return False, (
                "secure_keyguard_set — device has a PIN/pattern/password (or awaits "
                "post-reboot decryption); nerve cannot dismiss a secure keyguard. Provision "
                "it lock-free (Settings > Lock screen > None) or unlock it manually."
            )
        return False, "locked (keyguard up)"
    battery_ok = battery_pct is None or battery_pct >= BATTERY_FLOOR_PCT or charging
    if not battery_ok:
        return False, f"battery {battery_pct}% and not charging"
    return True, "ready"


# --- Connectivity (linc-nerve-up9) ---


class Transport(StrEnum):
    WIFI = "wifi"
    CELLULAR = "cellular"
    ETHERNET = "ethernet"
    VPN = "vpn"
    BLUETOOTH = "bluetooth"
    NONE = "none"  # no default network at all
    UNKNOWN = "unknown"  # parse failure / not checked — never a guessed value


@dataclass
class NetworkInfo:
    """Default-network state. Three-valued: None = could not be read, never a guess."""

    transport: Transport
    default_network: bool | None
    validated: bool | None
    reachable: bool | None = None

    @property
    def online(self) -> bool | None:
        """True only for a default network with validated internet; None if unreadable."""
        if self.default_network is None or self.validated is None:
            return None
        return self.default_network and self.validated

    def as_dict(self) -> dict[str, Any]:
        return {
            "transport": str(self.transport),
            "default_network": self.default_network,
            "validated": self.validated,
            "reachable": self.reachable,
            "online": self.online,
        }


def unknown_network() -> NetworkInfo:
    return NetworkInfo(Transport.UNKNOWN, None, None)


_TRANSPORT_MAP = {
    "WIFI": Transport.WIFI,
    "CELLULAR": Transport.CELLULAR,
    "ETHERNET": Transport.ETHERNET,
    "VPN": Transport.VPN,
    "BLUETOOTH": Transport.BLUETOOTH,
}


def _parse_transport(record: str) -> Transport:
    m = re.search(r"Transports:\s*([A-Z_|]+)", record)
    if not m:
        return Transport.UNKNOWN
    tokens = m.group(1).split("|")
    if "VPN" in tokens:  # a VPN rides an underlying transport; the VPN is what routes
        return Transport.VPN
    return _TRANSPORT_MAP.get(tokens[0], Transport.UNKNOWN)


def _parse_validated(record: str) -> bool | None:
    m = re.search(r"Capabilities:\s*([A-Z_&]+)", record)
    if not m:
        return None
    return "VALIDATED" in m.group(1).split("&")


def _agent_record(dump: str, net_id: str) -> str | None:
    """The single-line NetworkAgentInfo record for network ``net_id``, if printed."""
    needle = re.compile(r"NetworkAgentInfo\{.*?\bnetwork\{" + net_id + r"\}")
    return next((line for line in dump.splitlines() if needle.search(line)), None)


def parse_connectivity(dumpsys_connectivity: str) -> NetworkInfo:
    """Parse ``dumpsys connectivity`` into the default network's transport + validation.

    ``Active default network: none`` -> transport none, not default, not validated.
    A numeric id -> that network's ``NetworkAgentInfo`` record: its ``Transports:``
    token and whether ``VALIDATED`` is among its ``Capabilities:``. Anything the
    parser cannot read stays unknown/None (three-valued, like the parsers above).
    """
    m = re.search(r"Active default network:\s*(\S+)", dumpsys_connectivity)
    if not m:
        return unknown_network()
    net_id = m.group(1)
    if net_id.lower() == "none":
        return NetworkInfo(Transport.NONE, False, False)
    if not net_id.isdigit():
        return unknown_network()
    record = _agent_record(dumpsys_connectivity, net_id)
    if record is None:
        return NetworkInfo(Transport.UNKNOWN, True, None)
    return NetworkInfo(_parse_transport(record), True, _parse_validated(record))


def parse_ping(output: str) -> bool | None:
    """``ping -c 1`` summary -> True (received), False (lost), None (no summary)."""
    m = re.search(r"\d+ packets transmitted,\s*(\d+) (?:packets )?received", output)
    if not m:
        return None
    return int(m.group(1)) > 0


def apply_network_requirement(
    verdict: dict[str, Any], network: NetworkInfo, require: bool
) -> dict[str, Any]:
    """Attach ``network`` to an ensure_ready verdict; gate ``ready`` only when ``require``.

    The earliest failure wins: a verdict that is already not ready keeps its reason.
    """
    out = {**verdict, "network": network.as_dict()}
    if not require or not verdict.get("ready"):
        return out
    online = network.online
    if online is None:
        out["ready"] = False
        out["reason"] = "network_unknown — connectivity state could not be read"
    elif not online:
        out["ready"] = False
        out["reason"] = (
            f"no_network — no default network with validated internet (transport "
            f"{network.transport}); nerve never joins networks: fix the device's Wi-Fi "
            "or skip it"
        )
    return out
