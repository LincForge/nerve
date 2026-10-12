"""Android connectivity probe — default network transport + validated internet.

Read-only mixin (linc-nerve-up9): it never joins, toggles or configures a network.
Every shell string is a fixed module constant — no caller-supplied host or fragment.
"""

from __future__ import annotations

from . import android as _android
from .device_state import NetworkInfo, parse_connectivity, parse_ping, unknown_network

_CONNECTIVITY_CMD = "dumpsys connectivity"
_PING_CMD = "ping -c 1 -w 3 8.8.8.8"
_SHELL_TIMEOUT_S = 5


class _NetworkMixin:
    """``network_info`` for AndroidController (uses the controller's ``_shell``)."""

    def network_info(self, serial: str, probe: bool = False) -> NetworkInfo:
        """Default-network state; with ``probe`` also one ICMP ping to set ``reachable``.

        The ping runs only when the device has a default network. A failed
        connectivity read yields the all-unknown NetworkInfo; a failed ping keeps
        the connectivity result and leaves ``reachable`` None.
        """
        try:
            raw = self._shell(serial, _CONNECTIVITY_CMD, timeout=_SHELL_TIMEOUT_S)
            info = parse_connectivity(raw)
        except Exception as exc:
            _android.logger.debug("network_info_failed", serial=serial, error=str(exc))
            return unknown_network()
        if probe and info.default_network:
            info.reachable = self._probe_reachable(serial)
        return info

    def _probe_reachable(self, serial: str) -> bool | None:
        try:
            return parse_ping(self._shell(serial, _PING_CMD, timeout=_SHELL_TIMEOUT_S))
        except Exception as exc:
            _android.logger.debug("network_ping_failed", serial=serial, error=str(exc))
            return None
