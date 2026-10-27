"""Unit tests for ``core/android_apps.py`` package-only phone launch resolution.

Contract (linc-nerve-1mw2):
- ``resolve-activity`` names the package -> ``am start -n`` it, never query.
- ``resolve-activity`` does not name the package (ResolverActivity / no match)
  -> ``query-activities`` for MAIN/LAUNCHER: exactly one component is started,
  two or more raise ``RuntimeError`` naming every component (no monkey), zero
  keeps the LAUNCHER-monkey fallback.

Named ``test_android_apps.py`` so the diff-mutation gate's stem rule derives it
for ``core/android_apps.py`` (tests that only import ``core.android`` are not
derived, so they cannot kill this module's mutants).
"""

from __future__ import annotations

import pytest

from linc_nerve.core.android import AndroidController
from linc_nerve.core.android_apps import _AppsMixin

SERIAL = "ABC"
PKG = "com.example"
RESOLVER = (
    "priority=0 preferredOrder=0 match=0x0\nandroid/com.android.internal.app.ResolverActivity\n"
)
QUERY_CMD = (
    "cmd package query-activities --brief -a android.intent.action.MAIN "
    "-c android.intent.category.LAUNCHER com.example"
)
RESOLVE_CMD = "cmd package resolve-activity --brief com.example"
MONKEY_CMD = "monkey -p com.example -c android.intent.category.LAUNCHER 1"


def _ctrl(responses: dict[str, str]) -> tuple[AndroidController, list[tuple[str, str]]]:
    """Controller whose ``_shell`` answers by command prefix and records (serial, cmd)."""
    ctrl = AndroidController()
    calls: list[tuple[str, str]] = []

    def _shell(serial: str, cmd: str) -> str:
        calls.append((serial, cmd))
        for prefix, reply in responses.items():
            if cmd.startswith(prefix):
                return reply
        return ""

    ctrl._shell = _shell  # type: ignore[method-assign]
    return ctrl, calls


def _cmds(calls: list[tuple[str, str]]) -> list[str]:
    return [cmd for _, cmd in calls]


# --- _query_launcher_activities -------------------------------------------------


def test_query_launcher_activities_issues_exact_command_on_serial() -> None:
    ctrl, calls = _ctrl({"cmd package query-activities": "  com.example/.MainActivity\n"})

    assert ctrl._query_launcher_activities(SERIAL, PKG) == [".MainActivity"]
    assert calls == [(SERIAL, QUERY_CMD)]


def test_query_launcher_activities_returns_every_component_in_order() -> None:
    ctrl, _ = _ctrl(
        {
            "cmd package query-activities": (
                "2 activities found:\n"
                "  com.example/.MainActivity\n"
                "  com.example/.debug.LabActivity\n"
            )
        }
    )

    assert ctrl._query_launcher_activities(SERIAL, PKG) == [
        ".MainActivity",
        ".debug.LabActivity",
    ]


def test_query_launcher_activities_hostile_output_keeps_only_own_package() -> None:
    ctrl, _ = _ctrl(
        {
            "cmd package query-activities": (
                "\n   \n  com.other/.Foreign\n  com.example.other/.AlsoForeign\n"
                "  None/.NotThePackage\n  com.example/.MainActivity  \n\n"
            )
        }
    )

    assert ctrl._query_launcher_activities(SERIAL, PKG) == [".MainActivity"]


def test_query_launcher_activities_empty_output_is_empty_list() -> None:
    ctrl, _ = _ctrl({"cmd package query-activities": "No activities found\n"})

    assert ctrl._query_launcher_activities(SERIAL, PKG) == []


def test_parse_launcher_activities_ignores_foreign_and_blank_lines() -> None:
    output = "\n  android/com.android.internal.app.ResolverActivity\n com.example/.A\n\n"

    assert _AppsMixin._parse_launcher_activities(output, PKG) == [".A"]
    assert _AppsMixin._parse_resolved_activity(output, PKG) == ".A"
    assert _AppsMixin._parse_resolved_activity(RESOLVER, PKG) is None


# --- _launch_phone_package ------------------------------------------------------


def test_launch_phone_resolved_activity_starts_it_without_querying() -> None:
    ctrl, calls = _ctrl(
        {
            "cmd package resolve-activity": "com.example/.Resolved\n",
            "cmd package query-activities": "  com.example/.Other\n",
        }
    )

    ctrl._launch_phone_package(SERIAL, PKG)

    assert calls == [(SERIAL, RESOLVE_CMD), (SERIAL, "am start -n com.example/.Resolved")]


def test_launch_phone_two_launchers_raises_exact_message_without_monkey() -> None:
    ctrl, calls = _ctrl(
        {
            "cmd package resolve-activity": RESOLVER,
            "cmd package query-activities": (
                "  com.example/.MainActivity\n  com.example/.debug.LabActivity\n"
            ),
        }
    )

    with pytest.raises(RuntimeError) as exc_info:
        ctrl._launch_phone_package(SERIAL, PKG)

    assert str(exc_info.value) == (
        "com.example has 2 launcher activities: "
        "com.example/.MainActivity, com.example/.debug.LabActivity; "
        "pass activity= to choose one"
    )
    assert calls == [(SERIAL, RESOLVE_CMD), (SERIAL, QUERY_CMD)]


def test_launch_phone_three_launchers_raises_and_names_count() -> None:
    ctrl, calls = _ctrl(
        {
            "cmd package resolve-activity": RESOLVER,
            "cmd package query-activities": ("com.example/.A\ncom.example/.B\ncom.example/.C\n"),
        }
    )

    with pytest.raises(RuntimeError, match=r"^com\.example has 3 launcher activities: "):
        ctrl._launch_phone_package(SERIAL, PKG)

    assert not any("monkey" in cmd or cmd.startswith("am start") for cmd in _cmds(calls))


def test_launch_phone_single_queried_activity_starts_that_component() -> None:
    ctrl, calls = _ctrl(
        {
            "cmd package resolve-activity": RESOLVER,
            "cmd package query-activities": "1 activity found:\n  com.example/.MainActivity\n",
        }
    )

    ctrl._launch_phone_package(SERIAL, PKG)

    assert calls == [
        (SERIAL, RESOLVE_CMD),
        (SERIAL, QUERY_CMD),
        (SERIAL, "am start -n com.example/.MainActivity"),
    ]


def test_launch_phone_single_queried_activity_carries_extras() -> None:
    ctrl, calls = _ctrl(
        {
            "cmd package resolve-activity": RESOLVER,
            "cmd package query-activities": "com.example/.MainActivity\n",
        }
    )

    ctrl._launch_phone_package(SERIAL, PKG, "--ez SKIP_CONSENT true")

    assert _cmds(calls)[-1] == "am start -n com.example/.MainActivity --ez SKIP_CONSENT true"


def test_launch_phone_no_launchers_falls_back_to_monkey() -> None:
    ctrl, calls = _ctrl(
        {
            "cmd package resolve-activity": RESOLVER,
            "cmd package query-activities": "No activities found\n",
            "monkey": "Events injected: 1\n",
        }
    )

    ctrl._launch_phone_package(SERIAL, PKG)

    assert calls == [(SERIAL, RESOLVE_CMD), (SERIAL, QUERY_CMD), (SERIAL, MONKEY_CMD)]


def test_launch_phone_no_launchers_with_extras_refuses_monkey() -> None:
    ctrl, calls = _ctrl(
        {
            "cmd package resolve-activity": RESOLVER,
            "cmd package query-activities": "",
        }
    )

    with pytest.raises(RuntimeError, match="extras"):
        ctrl._launch_phone_package(SERIAL, PKG, "--ez SKIP_CONSENT true")

    assert _cmds(calls) == [RESOLVE_CMD, QUERY_CMD]


def test_launch_with_explicit_activity_never_resolves_or_queries() -> None:
    ctrl, calls = _ctrl({})

    ctrl.launch(SERIAL, PKG, activity=".X")

    assert calls == [(SERIAL, "am start -n com.example/.X")]
