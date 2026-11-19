"""Tests for nerve CLI long-operation timeout default and honest timeout hint (linc-nerve-frnc)."""

from __future__ import annotations

import ast
import inspect
import json
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from linc_cli_kit import HttpDaemonTransport, ToolError

from linc_nerve.cli.errors import honest_toolerror
from linc_nerve.cli.main import TRANSPORT, _spec_for, main
from linc_nerve.core import timeouts

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")

_EXPECTED_LONG_OP_HINT = (
    "the operation may still be running on the device; check its result "
    "(for install: `nerve apps <device>`) before retrying, or pass --timeout"
)


def test_nerve_transport_call_uses_cli_default_for_listed_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] NerveTransport.call substitutes timeouts.cli_default(spec.name) for timeout=None."""
    recorded: list[tuple[str, dict[str, Any], float | None]] = []

    def fake_super_call(
        self: HttpDaemonTransport,
        spec: Any,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        recorded.append((spec.name, dict(payload), timeout))
        return {"success": True}

    monkeypatch.setattr(HttpDaemonTransport, "call", fake_super_call)

    install_spec = _spec_for("nerve_install")
    tap_spec = _spec_for("nerve_tap")

    # Listed tool with timeout=None forwards 200 (> DAEMON_CEILING_S = 180.0).
    res_install_default = TRANSPORT.call(
        install_spec, {"device": "DEV1", "path": "/tmp/app.apk"}, timeout=None
    )
    assert res_install_default == {"success": True}

    # Explicit user --timeout is passed through untouched.
    res_install_explicit = TRANSPORT.call(
        install_spec, {"device": "DEV1", "path": "/tmp/app.apk"}, timeout=5.0
    )
    assert res_install_explicit == {"success": True}

    # Unlisted tool with timeout=None forwards None so the kit default (30.0) applies (pin).
    res_tap_default = TRANSPORT.call(tap_spec, {"device": "DEV1", "x": 10, "y": 20}, timeout=None)
    assert res_tap_default == {"success": True}

    # Unlisted tool with explicit timeout forwards that timeout (pin).
    res_tap_explicit = TRANSPORT.call(tap_spec, {"device": "DEV1", "x": 10, "y": 20}, timeout=12.5)
    assert res_tap_explicit == {"success": True}

    assert recorded == [
        ("nerve_install", {"device": "DEV1", "path": "/tmp/app.apk"}, 200),
        ("nerve_install", {"device": "DEV1", "path": "/tmp/app.apk"}, 5.0),
        ("nerve_tap", {"device": "DEV1", "x": 10, "y": 20}, None),
        ("nerve_tap", {"device": "DEV1", "x": 10, "y": 20}, 12.5),
    ]
    assert TRANSPORT.timeout == 30.0
    assert timeouts.cli_default("nerve_install") > timeouts.DAEMON_CEILING_S


@pytest.mark.parametrize(
    "listed_tool",
    [
        "nerve_install",
        "nerve_record_fixed",
        "nerve_record_stop",
        "nerve_emulator_start",
    ],
)
def test_every_listed_tool_cli_timeout_exceeds_daemon_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    listed_tool: str,
) -> None:
    """[contract] Effective CLI timeout for every listed tool exceeds the daemon ceiling."""
    recorded_timeouts: list[float | None] = []

    def fake_super_call(
        self: HttpDaemonTransport,
        spec: Any,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        recorded_timeouts.append(timeout)
        return {"ok": True}

    monkeypatch.setattr(HttpDaemonTransport, "call", fake_super_call)

    spec = _spec_for(listed_tool)
    TRANSPORT.call(spec, {"device": "DEV1"}, timeout=None)

    assert len(recorded_timeouts) == 1
    effective = recorded_timeouts[0]
    assert effective == timeouts.cli_default(listed_tool)
    assert effective is not None
    assert effective > timeouts.DAEMON_CEILING_S


def test_upstream_timeout_envelope_for_install_vs_tap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] upstream_timeout on nerve_install surfaces honest hint; nerve_tap unchanged."""

    def fake_super_call_timeout(
        self: HttpDaemonTransport,
        spec: Any,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        raise ToolError(
            f"nerve daemon timed out after {timeout}s at http://127.0.0.1:8111/tools/{spec.name}",
            code="UPSTREAM_TIMEOUT",
            category="upstream_timeout",
            hint="generic upstream timeout hint",
            retryable=True,
            details={"tool": spec.name},
            exit_code=1,
        )

    monkeypatch.setattr(HttpDaemonTransport, "call", fake_super_call_timeout)

    runner = CliRunner()

    # nerve install surfaces the "may still be running" hint with category and exit code unchanged.
    install_res = runner.invoke(main, ["install", "--device", "DEV1", "--path", "/tmp/app.apk"])
    assert install_res.exit_code == 1
    assert install_res.stdout == ""
    install_env = json.loads(install_res.stderr)["error"]
    assert install_env["code"] == "UPSTREAM_TIMEOUT"
    assert install_env["category"] == "upstream_timeout"
    assert install_env["retryable"] is True
    assert install_env["details"] == {"tool": "nerve_install"}
    assert install_env["hint"] == _EXPECTED_LONG_OP_HINT
    assert "may still be running" in install_env["hint"]
    assert "nerve apps <device>" in install_env["hint"]
    assert "--timeout" in install_env["hint"]

    # nerve tap keeps the original envelope unchanged (pin).
    tap_res = runner.invoke(main, ["tap", "DEV1", "100", "200"])
    assert tap_res.exit_code == 1
    assert tap_res.stdout == ""
    tap_env = json.loads(tap_res.stderr)["error"]
    assert tap_env["code"] == "UPSTREAM_TIMEOUT"
    assert tap_env["category"] == "upstream_timeout"
    assert tap_env["retryable"] is True
    assert tap_env["details"] == {"tool": "nerve_tap"}
    assert tap_env["hint"] == "generic upstream timeout hint"


def test_honest_toolerror_preserves_fields_and_ignores_other_categories() -> None:
    """[contract] honest_toolerror preserves exit_code, code, retryable, details for listed."""
    exc = ToolError(
        "timed out",
        code="UPSTREAM_TIMEOUT",
        hint="old hint",
        category="upstream_timeout",
        retryable=False,
        details={"attempt": 1},
        exit_code=3,
    )
    mapped = honest_toolerror(exc, "nerve_install")
    assert mapped is not exc
    assert mapped.message == "timed out"
    assert mapped.code == "UPSTREAM_TIMEOUT"
    assert mapped.category == "upstream_timeout"
    assert mapped.hint == _EXPECTED_LONG_OP_HINT
    assert mapped.retryable is False
    assert mapped.details == {"attempt": 1}
    assert mapped.exit_code == 3

    # Non-timeout category on a listed tool is returned unchanged.
    other_cat = ToolError(
        "daemon down",
        code="DAEMON_UNAVAILABLE",
        hint="start daemon",
        category="unavailable",
    )
    assert honest_toolerror(other_cat, "nerve_install") is other_cat

    # upstream_timeout on an unlisted tool or empty tool_name is returned unchanged.
    assert honest_toolerror(exc, "nerve_tap") is exc
    assert honest_toolerror(exc) is exc


@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), 0, -1, float("inf"), float("-inf"), True],
    ids=["nan", "zero", "neg_one", "inf", "neg_inf", "bool_true"],
)
def test_cli_default_raises_value_error_for_invalid_table_entries(bad_value: object) -> None:
    """[contract] timeouts.cli_default raises ValueError for NaN, 0, -1, non-finite, or bool."""
    with (
        patch.dict(timeouts.CLI_DEFAULT_S, {"nerve_install": bad_value}),  # type: ignore[dict-item]
        pytest.raises(ValueError, match="finite positive number"),
    ):
        timeouts.cli_default("nerve_install")


def test_timeouts_module_validates_at_import_without_assert() -> None:
    """[contract] timeouts module validates table at import time using ValueError, never assert."""
    source = inspect.getsource(timeouts)
    tree = ast.parse(source)
    assert_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]
    assert assert_nodes == []

    top_level_calls = [
        node.value.func.id
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]
    assert "validate_timeouts_table" in top_level_calls
