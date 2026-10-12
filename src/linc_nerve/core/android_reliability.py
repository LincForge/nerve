"""Android reliability primitives — circuit breaker, ADB restart, hard timeout, shell, metrics.

Split from android.py (#53) as a mixin to keep both files under the hot-file LOC
ceiling. References to test-patched / android.py module-globals (``subprocess``,
``time``, ``logger``, ``ADB_PATH``, ``classify_error``, ``CircuitState``,
``DeviceCircuitBreaker``, constants) are routed through the parent module
(``_android.<name>``) so patches on ``linc_nerve.core.android.*`` keep working.
"""

from __future__ import annotations

import time
from typing import Any

from . import adb_health
from . import android as _android
from .adb_server import adb_server_address


def _adb_restart_evidence(
    status: adb_health.AdbTransportStatus | None,
    now: float,
) -> str | None:
    """Return 'wedged' when status has fresh watchdog wedge evidence, else None."""
    if (
        status is not None
        and status.wedged
        and now - status.checked_at <= adb_health.STALE_AFTER_SECONDS
    ):
        return "wedged"
    return None


class _ReliabilityMixin:
    """ADB client plumbing, circuit breaker, recovery, timeout, shell, metrics."""

    def _get_client(self) -> Any:
        """Lazy-init the ADB client connection with health check.

        The server comes from ``adb_server_address()`` (ADB_SERVER_SOCKET /
        ANDROID_ADB_SERVER_PORT / 127.0.0.1:5037), resolved on every (re)connect.
        """
        if self._client is None or self._client_errors >= 2:
            import adbutils

            if self._client_errors >= 2:
                _android.logger.info("adb_client_reconnect", errors=self._client_errors)
            server = adb_server_address()
            self._client = adbutils.AdbClient(
                host=server.host, port=server.port, socket_timeout=10
            )
            self._client_errors = 0
        return self._client

    def _get_device(self, serial: str) -> Any:
        """Get an adbutils AdbDevice by serial."""
        return self._get_client().device(serial=serial)

    def _reset_client(self) -> None:
        """Reset the ADB client after repeated failures."""
        self._client = None
        self._client_errors = 0

    def _check_circuit(self, serial: str) -> None:
        """Raise DeviceError if device is in cooldown."""
        cb = self._circuit_breakers.get(serial)
        if cb is None or cb.state == _android.CircuitState.CLOSED:
            return
        if cb.state == _android.CircuitState.OPEN:
            if _android.time.monotonic() < cb.degraded_until:
                raise _android.DeviceError(
                    f"Device {serial} is degraded (circuit open), retry after cooldown"
                )
            # Cooldown expired → half-open, allow probe
            cb.state = _android.CircuitState.HALF_OPEN
            cb.failure_count = 0
            cb.degraded_until = None
        # HALF_OPEN: allow through (probe attempt)

    def _record_device_failure(self, serial: str) -> None:
        """Track per-device failure; open circuit at threshold.

        Triggers ADB restart on multi-device failure only when fresh watchdog
        wedge evidence is present.
        """
        cb = self._circuit_breakers.setdefault(serial, _android.DeviceCircuitBreaker())
        cb.failure_count += 1

        if cb.failure_count >= _android._CIRCUIT_FAILURE_THRESHOLD:
            cb.state = _android.CircuitState.OPEN
            cb.degraded_until = _android.time.monotonic() + _android._CIRCUIT_COOLDOWN_SECS
            _android.logger.warning(
                "circuit_opened", serial=serial, cooldown_s=_android._CIRCUIT_COOLDOWN_SECS
            )

        # Cross-device ADB restart tracking (unchanged)
        now = _android.time.monotonic()
        self._recent_failures[serial] = now
        self._recent_failures = {
            s: t
            for s, t in self._recent_failures.items()
            if now - t <= _android._FAILURE_WINDOW_SECS
        }
        if len(self._recent_failures) >= _android._DISTINCT_DEVICE_RESTART:
            reason = _adb_restart_evidence(adb_health.get_transport_status(), time.time())
            if reason is None:
                _android.logger.info(
                    "adb_server_restart_skipped",
                    distinct_devices=len(self._recent_failures),
                    reason="no_wedge_evidence",
                )
                self._recent_failures.clear()
            else:
                self._restart_adb_server()
                self._recent_failures.clear()

    def _clear_device_failure(self, serial: str) -> None:
        """Clear circuit breaker state on success."""
        self._circuit_breakers.pop(serial, None)

    def _restart_adb_server(self) -> None:
        """Restart ADB server as last resort after repeated cross-device failures.

        Delegates to ``adb_health.recover_adb_server``, which signals the adb
        server pid directly. The previous implementation ran ``adb kill-server``
        — itself an adb client, which blocks on the same socket a wedged server
        is spinning behind. It therefore could not repair the exact outage it
        existed for (linc-nerve#131); against a busy-spinning server it merely
        burned its own 5s timeout and moved on.
        """
        from linc_nerve.core import adb_health

        _android.logger.warning(
            "adb_server_restart",
            distinct_devices=len(self._recent_failures),
        )
        try:
            healed = adb_health.recover_adb_server(adb_health.find_adb_server_pid())
            self._reset_client()
            _android.logger.info("adb_server_restarted", healed=healed)
        except Exception as exc:
            _android.logger.error("adb_server_restart_failed", error=str(exc))

    def _hard_timeout(
        self,
        fn: Any,
        *,
        timeout: int,
        label: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Run *fn* with a hard timeout via daemon thread.

        Prevents indefinite hangs from adbutils socket reads, scrcpy stream
        startup, or ``d.screenshot()`` — all of which can ignore their own
        timeout parameter. The daemon thread is abandoned on timeout (no
        join, no deadlock).
        """
        result_q: _android.queue.Queue[tuple[str, Any]] = _android.queue.Queue()
        kw = kwargs or {}

        def worker() -> None:
            try:
                result_q.put(("ok", fn(*args, **kw)))
            except Exception as exc:
                result_q.put(("error", exc))

        t = _android.threading.Thread(target=worker, daemon=True, name=label)
        t.start()

        try:
            status, value = result_q.get(timeout=timeout)
        except _android.queue.Empty:
            _android.logger.warning("hard_timeout", label=label, timeout_s=timeout)
            raise TimeoutError(f"{label} hard-timed out after {timeout}s") from None

        if status == "ok":
            return value
        raise value

    def _shell(self, serial: str, cmd: str, timeout: int = 15) -> str:
        """Run a shell command with per-device lock, circuit breaker, retry, and ADB recovery.

        Checks per-device circuit breaker before attempting command. Retries up
        to ``_MAX_RETRIES`` times on transient errors with exponential backoff.
        Opens circuit after ``_CIRCUIT_FAILURE_THRESHOLD`` consecutive failures
        on the same device. Restarts ADB server when ``_DISTINCT_DEVICE_RESTART``
        distinct devices fail within ``_FAILURE_WINDOW_SECS``.
        """
        with self._device_lock(serial):
            self._check_circuit(serial)
            t0 = _android.time.monotonic()
            last_exc: BaseException | None = None

            for attempt in range(_android._MAX_RETRIES + 1):
                if attempt > 0:
                    backoff = _android._BACKOFF_BASE * (2 ** (attempt - 1))
                    _android.logger.info(
                        "adb_shell_retry",
                        serial=serial,
                        cmd=cmd.split()[0],
                        attempt=attempt + 1,
                        backoff_s=backoff,
                    )
                    _android.time.sleep(backoff)

                try:
                    d = self._get_device(serial)
                    result = self._hard_timeout(
                        d.shell,
                        args=(cmd,),
                        kwargs={"timeout": timeout},
                        timeout=timeout + 2,
                        label=f"adb-{cmd.split()[0]}",
                    )
                    self._client_errors = 0
                    self._clear_device_failure(serial)
                    self._record_metric(cmd, serial, t0, success=True)
                    return result
                except _android._RETRYABLE_ERRORS as exc:
                    last_exc = exc
                    self._client_errors += 1
                    self._record_device_failure(serial)
                    _android.logger.warning(
                        "adb_shell_failed",
                        serial=serial,
                        cmd=cmd.split()[0],
                        error=str(exc),
                        error_category=_android.classify_error(exc).value,
                        attempt=attempt + 1,
                        client_errors=self._client_errors,
                    )

            # All retries exhausted
            self._record_metric(cmd, serial, t0, success=False, error=last_exc)
            raise last_exc  # type: ignore[misc]

    def _record_metric(
        self,
        cmd: str,
        serial: str,
        t0: float,
        *,
        success: bool,
        error: BaseException | None = None,
    ) -> None:
        """Record a command metric with optional error categorization."""
        duration_ms = (_android.time.monotonic() - t0) * 1000
        if duration_ms > 2000:
            _android.logger.warning(
                "slow_command",
                serial=serial,
                cmd=cmd.split()[0],
                duration_ms=round(duration_ms, 1),
            )
        if self._metrics is not None:
            from .metrics import CommandMetric

            self._metrics.record(
                CommandMetric(
                    command=cmd.split()[0],
                    serial=serial,
                    duration_ms=round(duration_ms, 1),
                    success=success,
                    timestamp=_android.time.time(),
                    error=str(error) if error else None,
                    error_category=_android.classify_error(error) if error else None,
                )
            )
