"""[contract] Tool failures reach the agent with their cause on mcp 2.x (mcp-2x molecule,
toolerror-boundary).

From mcp 2.1 on, an ordinary exception raised in a tool surfaces to the client only as
``Error executing tool <name>`` (``UnexpectedToolError``, text hidden). nerve's server class
(``linc_nerve.mcp._mcpserver.NerveMCPServer``) re-raises such a crash as the SDK ``ToolError``
carrying ``"<ExceptionClass>: <message>"`` with home paths shown as ``~`` and the text capped at
500 chars, while the device_tool / global_tool metrics and log_mcp_call bookkeeping still record
the call as failed.

Two layers are exercised: nerve's registered tools on the live server, and probe tools that
each test registers on a scratch ``NerveMCPServer`` at test time.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock

import anyio
import pytest
from linc_cli_kit import SDKToolError
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import UnexpectedToolError

import linc_nerve.core.ios  # noqa: F401  (conftest imports ios_discovery; break the import cycle)
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.emulator import EmulatorManager
from linc_nerve.core.ios import IOSController
from linc_nerve.core.metrics import MetricsCollector
from linc_nerve.mcp import _core, _mcpserver
from linc_nerve.mcp._mcpserver import NerveMCPServer
from linc_nerve.mcp.diagnostic_tools import _nerve_session_stats_sync  # noqa: F401  (registers)
from linc_nerve.mcp.screenshot_tools import _nerve_screenshot_sync  # noqa: F401  (registers)

# Built at runtime: the public-safe gate (tests/test_public_safe.py) flags literal macOS
# user-home paths in tracked files.
_MAC_HOME = "/" + "/".join(("Users", "someone"))
_LEAKY = f"device PIXEL8PRO not found under {_MAC_HOME}/x"


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, str | None]] = []

    def __call__(
        self,
        server: str,
        tool: str,
        params: dict[str, Any],
        duration_ms: float,
        success: bool,
        error: str | None = None,
    ) -> None:
        self.calls.append((tool, success, error))


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[MagicMock, MagicMock, _Recorder]]:
    """Fake controllers + metrics + log sink installed as nerve's singletons."""
    if _core._device_mgr is not None:
        with contextlib.suppress(Exception):
            _core._device_mgr.close()
    android = MagicMock(spec=AndroidController)
    android.discover.return_value = [
        NerveDevice(
            serial="PIXEL8PRO",
            platform=Platform.ANDROID,
            device_type=DeviceType.PHONE,
            model="Pixel 8 Pro",
            os_version="14",
        )
    ]
    ios = MagicMock(spec=IOSController)
    ios.discover.return_value = []
    metrics = MagicMock(spec=MetricsCollector)
    device_mgr = MagicMock(spec=DeviceManager)
    device_mgr.guard.return_value = None
    monkeypatch.setattr(_core, "_android", android)
    monkeypatch.setattr(_core, "_ios", ios)
    monkeypatch.setattr(_core, "_metrics", metrics)
    monkeypatch.setattr(_core, "_device_mgr", device_mgr)
    monkeypatch.setattr(_core, "_emulator", MagicMock(spec=EmulatorManager))
    monkeypatch.setattr(_core, "_cached_devices", [])
    monkeypatch.setattr(_core, "_cache_timestamp", 0.0)
    recorder = _Recorder()
    monkeypatch.setattr(_core, "log_mcp_call", recorder)
    yield android, metrics, recorder


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch) -> NerveMCPServer:
    """A throwaway server + registry, so probe tools never reach nerve's real tool table."""
    srv = NerveMCPServer("scratch")
    monkeypatch.setattr(_core, "server", srv)
    monkeypatch.setattr(_core, "TOOL_REGISTRY", dict(_core.TOOL_REGISTRY))
    return srv


def _probe(body: Callable[[], Any], *, device: bool) -> str:
    """Register a nerve-wrapped probe tool whose body runs *body*; return its MCP name."""
    if device:

        def _probe_device_sync(device: str) -> Any:
            """Probe device tool."""
            return body()

        _core.device_tool(_probe_device_sync)
        return "probe_device"

    def _probe_global_sync() -> Any:
        """Probe global tool."""
        return body()

    _core.global_tool(_probe_global_sync)
    return "probe_global"


def _probe_args(device: bool) -> dict[str, Any]:
    return {"device": "PIXEL8PRO"} if device else {}


def _raiser(exc: BaseException) -> Callable[[], Any]:
    def body() -> Any:
        raise exc

    return body


class _RaisingSessions:
    """A state manager whose ``active_sessions`` raises the configured exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    @property
    def active_sessions(self) -> dict[str, Any]:
        raise self._exc


class _UnprintableError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("str() exploded")


def _call(srv: Any, name: str, arguments: dict[str, Any]) -> Any:
    return anyio.run(srv.call_tool, name, arguments)


def _tool_error(srv: Any, name: str, arguments: dict[str, Any]) -> SDKToolError:
    with pytest.raises(SDKToolError) as excinfo:
        _call(srv, name, arguments)
    err = excinfo.value
    assert not isinstance(err, UnexpectedToolError), f"{name}: cause hidden: {err!r}"
    return err


def _raw_tool(srv: NerveMCPServer, exc: BaseException) -> str:
    """Register a plain (unwrapped) tool on *srv* that raises *exc*."""

    def raw_boom() -> str:
        """Raw probe."""
        raise exc

    srv.add_tool(raw_boom)
    return "raw_boom"


# ---------------------------------------------------------------------------
# nerve's own registered tools on the live server
# ---------------------------------------------------------------------------


def test_contract_live_server_is_nerve_server() -> None:
    assert type(_core.server) is NerveMCPServer


def test_contract_device_tool_failure_reaches_agent_with_cause_and_no_home_path(
    fakes: tuple[MagicMock, MagicMock, _Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The device call raises on its worker thread (``_run_with_timeout`` re-raises it).

    ``_nerve_screenshot_sync`` turns a controller exception into an ``{"error": ...}`` dict
    itself, so the escape path the boundary guards is simulated one level down: the
    per-device dispatch re-raising whatever the device function raised.
    """
    _, metrics, recorder = fakes

    def _device_call_raises(fn: Any, serial: str, *_: Any, **__: Any) -> Any:
        raise RuntimeError(_LEAKY)

    monkeypatch.setattr(_core, "_run_with_timeout", _device_call_raises)

    err = _tool_error(_core.server, "nerve_screenshot", {"device": "PIXEL8PRO"})
    assert str(err) == (
        "Error executing tool nerve_screenshot: RuntimeError: device PIXEL8PRO not found under ~/x"
    )
    assert isinstance(err.__cause__, RuntimeError)
    metrics.record_tool_call.assert_called_with("nerve_screenshot", False)
    assert recorder.calls[-1] == ("nerve_screenshot", False, _LEAKY)


def test_contract_global_tool_failure_reaches_agent_with_cause(
    fakes: tuple[MagicMock, MagicMock, _Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, metrics, recorder = fakes
    raw = "bad session table under /home/ci/s"
    monkeypatch.setattr(_core, "_state_mgr", _RaisingSessions(ValueError(raw)))

    err = _tool_error(_core.server, "nerve_session_stats", {})
    assert str(err).endswith(": ValueError: bad session table under ~/s")
    assert "/home/ci" not in str(err)
    metrics.record_tool_call.assert_called_with("nerve_session_stats", False)
    assert recorder.calls[-1] == ("nerve_session_stats", False, raw)


def test_contract_tool_failure_over_the_wire_is_error_result_with_cause(
    fakes: tuple[MagicMock, MagicMock, _Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through a real client session the crash arrives as ``is_error`` with the cause text."""
    from mcp import Client

    monkeypatch.setattr(_core, "_state_mgr", _RaisingSessions(KeyError("sessions")))

    async def scenario() -> Any:
        async with Client(_core.server) as client:
            return await client.call_tool("nerve_session_stats", {})

    result = anyio.run(scenario)
    assert result.is_error is True
    assert result.content[0].text == (
        "Error executing tool nerve_session_stats: KeyError: 'sessions'"
    )


# ---------------------------------------------------------------------------
# Probe tools registered at test time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("device", [False, True], ids=["global", "device"])
def test_contract_probe_failure_is_tool_error_with_cause(
    fakes: tuple[MagicMock, MagicMock, _Recorder], scratch: NerveMCPServer, device: bool
) -> None:
    _, metrics, recorder = fakes
    boom = RuntimeError(_LEAKY)
    name = _probe(_raiser(boom), device=device)

    err = _tool_error(scratch, name, _probe_args(device))
    assert err.__cause__ is boom
    assert str(err) == (
        f"Error executing tool {name}: RuntimeError: device PIXEL8PRO not found under ~/x"
    )
    metrics.record_tool_call.assert_called_once_with(name, False)
    assert recorder.calls == [(name, False, _LEAKY)]


@pytest.mark.parametrize("device", [False, True], ids=["global", "device"])
def test_contract_probe_long_message_is_capped(
    fakes: tuple[MagicMock, MagicMock, _Recorder], scratch: NerveMCPServer, device: bool
) -> None:
    _, metrics, recorder = fakes
    raw = "z" * 10_000
    name = _probe(_raiser(RuntimeError(raw)), device=device)

    err = _tool_error(scratch, name, _probe_args(device))
    sent = str(err).removeprefix(f"Error executing tool {name}: ")
    assert len(sent) == _mcpserver.CLIENT_MSG_MAX == 500
    assert sent.startswith("RuntimeError: zzz")
    metrics.record_tool_call.assert_called_once_with(name, False)
    assert recorder.calls == [(name, False, raw)]


@pytest.mark.parametrize("device", [False, True], ids=["global", "device"])
def test_contract_probe_tool_error_passes_through_unchanged(
    fakes: tuple[MagicMock, MagicMock, _Recorder], scratch: NerveMCPServer, device: bool
) -> None:
    _, metrics, recorder = fakes
    original = SDKToolError("pick a device first")
    name = _probe(_raiser(original), device=device)

    err = _tool_error(scratch, name, _probe_args(device))
    assert err.__cause__ is original
    assert str(err) == f"Error executing tool {name}: pick a device first"
    metrics.record_tool_call.assert_called_once_with(name, False)
    assert recorder.calls == [(name, False, "pick a device first")]


@pytest.mark.parametrize("device", [False, True], ids=["global", "device"])
def test_probe_success_and_validation_error_dict_bookkeeping(
    fakes: tuple[MagicMock, MagicMock, _Recorder], scratch: NerveMCPServer, device: bool
) -> None:
    """Pin: a normal result and a validation-error dict still count as successes."""
    _, metrics, recorder = fakes
    results: list[dict[str, Any]] = [
        {"ok": 1},
        {"error": "bad app_id", "validation_error": True},
        {"error": "device fell over"},
    ]
    name = _probe(lambda: results.pop(0), device=device)

    for expected_success, expected_error in (
        (True, None),
        (True, "bad app_id"),
        (False, "device fell over"),
    ):
        result = _call(scratch, name, _probe_args(device))
        assert result.is_error is False
        metrics.record_tool_call.assert_called_with(name, expected_success)
        assert recorder.calls[-1] == (name, expected_success, expected_error)


_ROUNDS = 300


@pytest.mark.parametrize("device", [False, True], ids=["global", "device"])
def test_contract_bookkeeping_stays_exact_over_mixed_outcomes(
    fakes: tuple[MagicMock, MagicMock, _Recorder], scratch: NerveMCPServer, device: bool
) -> None:
    """[contract] Over many calls mixing success, crash and ToolError, every call is counted once.

    Metrics and the call log agree call-for-call with what the client saw: successes recorded
    True, both failure kinds recorded False with their own text, and every failure surfacing
    as an anticipated ToolError (never ``UnexpectedToolError``).
    """
    _, metrics, recorder = fakes
    outcomes = [i % 3 for i in range(_ROUNDS)]  # 0 ok, 1 crash, 2 ToolError
    feed = iter(outcomes)

    def body() -> Any:
        kind = next(feed)
        if kind == 1:
            raise OSError(f"{_MAC_HOME}/dev/{kind}")
        if kind == 2:
            raise SDKToolError("try another device")
        return {"ok": True}

    name = _probe(body, device=device)
    args = _probe_args(device)

    async def drive() -> list[str]:
        seen: list[str] = []
        for _ in outcomes:
            try:
                await scratch.call_tool(name, args)
                seen.append("ok")
            except UnexpectedToolError:
                seen.append("hidden")
            except SDKToolError as err:
                seen.append(str(err).removeprefix(f"Error executing tool {name}: "))
        return seen

    seen = anyio.run(drive)
    assert seen == ["ok", "OSError: ~/dev/1", "try another device"] * (_ROUNDS // 3)
    flags = [c.args for c in metrics.record_tool_call.call_args_list]
    assert flags == [(name, kind == 0) for kind in outcomes]
    assert [(t, ok) for t, ok, _ in recorder.calls] == [(name, kind == 0) for kind in outcomes]
    assert [e for _, _, e in recorder.calls][:3] == [
        None,
        f"{_MAC_HOME}/dev/1",
        "try another device",
    ]


# ---------------------------------------------------------------------------
# NerveMCPServer.call_tool on plain tools (hostile exception shapes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(ValueError("x"), "ValueError: x", id="plain"),
        pytest.param(OSError(f"{_MAC_HOME}/b and /home/c/d"), "OSError: ~/b and ~/d", id="homes"),
        pytest.param(_UnprintableError(), "_UnprintableError", id="unprintable"),
        pytest.param(RuntimeError(""), "RuntimeError: ", id="empty"),
        pytest.param(OSError(f"'{_MAC_HOME}'"), "OSError: '~'", id="quoted-home"),
    ],
)
def test_contract_crash_text_shapes(exc: BaseException, expected: str) -> None:
    srv = NerveMCPServer("scratch")
    name = _raw_tool(srv, exc)
    err = _tool_error(srv, name, {})
    assert str(err) == f"Error executing tool {name}: {expected}"
    assert err.__cause__ is exc


def test_contract_non_standard_home_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """$HOME outside /Users and /home (e.g. a service account) is still shown as ``~``."""
    monkeypatch.setenv("HOME", "/opt/svcX/")
    srv = NerveMCPServer("scratch")
    name = _raw_tool(srv, OSError("/opt/svcX/k"))
    assert str(_tool_error(srv, name, {})).endswith(": OSError: ~/k")


@pytest.mark.parametrize("home", ["/", ""])
def test_contract_root_or_empty_home_is_not_redacted(
    monkeypatch: pytest.MonkeyPatch, home: str
) -> None:
    monkeypatch.setenv("HOME", home)
    srv = NerveMCPServer("scratch")
    name = _raw_tool(srv, OSError("/opt/x"))
    assert str(_tool_error(srv, name, {})).endswith(": OSError: /opt/x")


def test_contract_unset_home_still_redacts_user_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOME", raising=False)
    srv = NerveMCPServer("scratch")
    name = _raw_tool(srv, OSError("/home/u/x XXXX"))
    assert str(_tool_error(srv, name, {})).endswith(": OSError: ~/x XXXX")


def test_contract_validation_and_unknown_tool_errors_unchanged() -> None:
    """Anticipated SDK ToolErrors (bad args, unknown tool) are not rewritten."""
    srv = NerveMCPServer("scratch")

    def typed(n: int) -> int:
        """Typed probe."""
        return n

    srv.add_tool(typed)
    bad = _tool_error(srv, "typed", {"n": "not-an-int"})
    assert str(bad).startswith("Error executing tool typed: ")
    assert "validation error" in str(bad)
    with pytest.raises(SDKToolError) as unknown:
        _call(srv, "no_such_tool", {})
    assert "no_such_tool" in str(unknown.value)


def test_contract_nested_or_causeless_crash_stays_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An UnexpectedToolError caused by a ToolError (nested crash), or with no cause, passes."""
    srv = NerveMCPServer("scratch")
    name = _raw_tool(srv, UnexpectedToolError("Error executing tool inner"))
    with pytest.raises(UnexpectedToolError) as excinfo:
        _call(srv, name, {})
    assert isinstance(excinfo.value.__cause__, SDKToolError)

    async def no_cause(*_: Any, **__: Any) -> Any:
        raise UnexpectedToolError("Error executing tool bare")

    monkeypatch.setattr(_mcpserver.MCPServer, "call_tool", no_cause)
    with pytest.raises(UnexpectedToolError, match=r"^Error executing tool bare$"):
        _call(srv, "anything", {})


def test_home_dir_pattern_stops_at_the_user_segment() -> None:
    assert _mcpserver.HOME_DIR_RE.sub("~", f"/home/u/x '{_MAC_HOME}'") == "~/x '~'"


def test_contract_caller_context_reaches_the_tool() -> None:
    """``call_tool`` forwards the caller's Context instead of building a fresh one."""
    srv = NerveMCPServer("scratch")

    def ctxprobe(ctx: Context) -> str:
        """Context probe."""
        return str(id(ctx))

    srv.add_tool(ctxprobe)
    ctx = Context(mcp_server=srv)
    result = anyio.run(srv.call_tool, "ctxprobe", {}, ctx)
    assert result.content[0].text == str(id(ctx))
