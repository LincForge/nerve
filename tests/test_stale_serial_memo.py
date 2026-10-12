"""Tests for per-serial memo in _stale_ready_android.

When the device cache is past _CACHE_TTL (5 s) and not invalidated,
_stale_ready_android resolves known Android devices via a cheap adb
liveness check (is_ready_serial). Successful checks are memoised per serial
for _CACHE_TTL so subsequent calls do not repeat the probe.

Times are written as ``T0 + t``: the cache is filled at relative t=0. T0 is
non-zero because a ``_cache_timestamp`` of 0.0 is the invalidated sentinel.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.mcp import _core

T0 = 1000.0


def _tab() -> NerveDevice:
    return NerveDevice(
        serial="S",
        platform=Platform.ANDROID,
        device_type=DeviceType.TABLET,
        model="SM-T570",
        os_version="12",
    )


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FakeAndroid:
    def __init__(self, devices: list[NerveDevice], ready: set[str]) -> None:
        self.devices = devices
        self.ready = ready
        self.discover_calls = 0
        self.ready_checks: list[str] = []

    def discover(self) -> list[NerveDevice]:
        self.discover_calls += 1
        return list(self.devices)

    def is_ready_serial(self, serial: str) -> bool:
        self.ready_checks.append(serial)
        return serial in self.ready


class _FakeIOS:
    def __init__(self, devices: list[NerveDevice] | None = None) -> None:
        self.devices = devices or []
        self.discover_calls = 0

    def discover(self) -> list[NerveDevice]:
        self.discover_calls += 1
        return list(self.devices)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock(now=T0)
    monkeypatch.setattr(_core, "_clock", fake)
    return fake


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> Any:
    """Install fake controllers and a cache filled at relative t=0 (``T0``)."""

    def _wire(
        android: _FakeAndroid,
        ios: _FakeIOS,
        cached: list[NerveDevice],
    ) -> tuple[_FakeAndroid, _FakeIOS]:
        device_mgr = MagicMock()
        device_mgr.guard.return_value = None
        monkeypatch.setattr(_core, "_android", android)
        monkeypatch.setattr(_core, "_ios", ios)
        monkeypatch.setattr(_core, "_state_mgr", MagicMock())
        monkeypatch.setattr(_core, "_metrics", MagicMock())
        monkeypatch.setattr(_core, "_device_mgr", device_mgr)
        monkeypatch.setattr(_core, "_emulator", MagicMock())
        monkeypatch.setattr(_core, "_cached_devices", list(cached))
        monkeypatch.setattr(_core, "_cache_timestamp", T0)
        return android, ios

    return _wire


def test_stale_serial_memo_probes_once_and_reprobes_after_ttl(
    wire: Any, clock: _FakeClock
) -> None:
    """Cache filled at t=0, calls at t=6, 7, 8 probe once; at t=12 probes again."""
    android, ios = wire(_FakeAndroid([_tab()], {"S"}), _FakeIOS(), [_tab()])

    # t=6: initial probe on expired cache
    clock.now = T0 + 6.0
    dev, _ = _core._resolve_device("S")
    assert dev is not None and dev.serial == "S"
    assert android.ready_checks == ["S"]

    # t=7, t=8: memo is valid within _CACHE_TTL (5 s), no additional probe
    clock.now = T0 + 7.0
    dev, _ = _core._resolve_device("S")
    assert dev is not None and dev.serial == "S"
    assert android.ready_checks == ["S"]

    clock.now = T0 + 8.0
    dev, _ = _core._resolve_device("S")
    assert dev is not None and dev.serial == "S"
    assert android.ready_checks == ["S"]

    # t=12: memo older than TTL (12.0 - 6.0 = 6.0 > 5.0), probes again
    clock.now = T0 + 12.0
    dev, _ = _core._resolve_device("S")
    assert dev is not None and dev.serial == "S"
    assert android.ready_checks == ["S", "S"]

    assert android.discover_calls == 0
    assert ios.discover_calls == 0


def test_invalidate_cache_clears_memo_and_forces_full_refresh(
    wire: Any, clock: _FakeClock
) -> None:
    """After _invalidate_cache() the next call does not use memo and refreshes fully."""
    restarted = NerveDevice(
        serial="S",
        platform=Platform.ANDROID,
        device_type=DeviceType.TABLET,
        model="SM-T570-RESTARTED",
        os_version="13",
    )
    android, ios = wire(
        _FakeAndroid([restarted], {"S"}),
        _FakeIOS(),
        [_tab()],
    )

    # t=6: resolve and memoise
    clock.now = T0 + 6.0
    dev, _ = _core._resolve_device("S")
    assert dev is not None and dev.model == "SM-T570"
    assert android.ready_checks == ["S"]

    # Explicit invalidation
    _core._invalidate_cache()

    # Next call at t=7 must not use memo; full refresh path runs
    clock.now = T0 + 7.0
    dev, _ = _core._resolve_device("S")
    assert dev is not None and dev.model == "SM-T570-RESTARTED"
    assert android.discover_calls == 1
    assert ios.discover_calls == 1
    # is_ready_serial was not called during full refresh
    assert android.ready_checks == ["S"]


def test_probe_returning_false_is_never_memoised(wire: Any, clock: _FakeClock) -> None:
    """A probe returning False is never memoised."""
    android, _ = wire(_FakeAndroid([], set()), _FakeIOS(), [_tab()])

    clock.now = T0 + 6.0
    dev, err = _core._resolve_device("S")
    assert dev is None
    assert "not found" in err
    assert android.ready_checks == ["S"]

    if hasattr(_core, "_stale_confirmed"):
        assert "S" not in _core._stale_confirmed

    # Second call at t=7 still probes / tries refresh, not answered from memo
    clock.now = T0 + 7.0
    dev, err = _core._resolve_device("S")
    assert dev is None
    assert "not found" in err
    if hasattr(_core, "_stale_confirmed"):
        assert "S" not in _core._stale_confirmed


def test_probe_raising_exception_is_never_memoised(wire: Any, clock: _FakeClock) -> None:
    """A probe raising an exception is never memoised."""
    android, _ = wire(_FakeAndroid([_tab()], {"S"}), _FakeIOS(), [_tab()])

    def _boom(serial: str) -> bool:
        android.ready_checks.append(serial)
        raise ConnectionError("adb connection lost")

    android.is_ready_serial = _boom  # type: ignore[method-assign]

    clock.now = T0 + 6.0
    # Resolves via fallback full refresh after probe error
    dev, _ = _core._resolve_device("S")
    assert dev is not None and dev.serial == "S"
    assert android.ready_checks == ["S"]
    assert android.discover_calls == 1

    if hasattr(_core, "_stale_confirmed"):
        assert "S" not in _core._stale_confirmed


def test_memo_is_void_once_the_cached_entry_is_replaced(
    wire: Any, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A memo confirms one cached NerveDevice; a replaced cache entry is probed afresh."""
    android, _ = wire(_FakeAndroid([_tab()], {"S"}), _FakeIOS(), [_tab()])

    clock.now = T0 + 6.0
    assert _core._resolve_device("S")[0] is not None
    assert android.ready_checks == ["S"]

    # Another path swaps the snapshot (same serial, new entry) without a refresh.
    monkeypatch.setattr(_core, "_cached_devices", [_tab()])
    clock.now = T0 + 7.0
    assert _core._resolve_device("S")[0] is _core._cached_devices[0]
    assert android.ready_checks == ["S", "S"]
