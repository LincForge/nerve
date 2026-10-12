"""MJPEG port pool + xctestrun MJPEG injection."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from linc_nerve.core.wda import (
    _MJPEG_PORT_MAX,
    _MJPEG_PORT_MIN,
    _allocate_mjpeg_port,
    _prepare_xctestrun,
    _release_mjpeg_port,
    _reset_mjpeg_port_state,
)


@pytest.fixture(autouse=True)
def _clean_mjpeg_ports():
    _reset_mjpeg_port_state()
    yield
    _reset_mjpeg_port_state()


def test_first_allocation_is_pool_min():
    assert _allocate_mjpeg_port("serialA") == _MJPEG_PORT_MIN


def test_same_serial_returns_same_port():
    p1 = _allocate_mjpeg_port("serialA")
    p2 = _allocate_mjpeg_port("serialA")
    assert p1 == p2


def test_distinct_serials_get_distinct_ports():
    assert _allocate_mjpeg_port("A") != _allocate_mjpeg_port("B")


def test_released_port_is_recycled():
    p = _allocate_mjpeg_port("A")
    _release_mjpeg_port("A")
    assert _allocate_mjpeg_port("B") == p


def test_pool_exhaustion_raises_runtimeerror():
    for i in range(_MJPEG_PORT_MIN, _MJPEG_PORT_MAX + 1):
        _allocate_mjpeg_port(f"s{i}")
    with pytest.raises(RuntimeError, match="MJPEG port pool exhausted"):
        _allocate_mjpeg_port("overflow")


# --- Task 2: xctestrun injection ---


def _write_xctestrun(tmp_path: Path) -> Path:
    plist = {
        "WebDriverAgentRunner-Runner": {
            "EnvironmentVariables": {"EXISTING": "1"},
        }
    }
    p = tmp_path / "in.xctestrun"
    with open(p, "wb") as f:
        plistlib.dump(plist, f)
    return p


def test_prepare_xctestrun_injects_both_ports(tmp_path):
    src = _write_xctestrun(tmp_path)
    out = _prepare_xctestrun(src, 8100, 9100)
    with open(out, "rb") as f:
        env = plistlib.load(f)["WebDriverAgentRunner-Runner"]["EnvironmentVariables"]
    assert env["USE_PORT"] == "8100"
    assert env["MJPEG_SERVER_PORT"] == "9100"
    out.unlink()


def test_prepare_xctestrun_omits_mjpeg_when_port_none(tmp_path):
    src = _write_xctestrun(tmp_path)
    out = _prepare_xctestrun(src, 8100, None)
    with open(out, "rb") as f:
        env = plistlib.load(f)["WebDriverAgentRunner-Runner"]["EnvironmentVariables"]
    assert env["USE_PORT"] == "8100"
    assert "MJPEG_SERVER_PORT" not in env
    out.unlink()
