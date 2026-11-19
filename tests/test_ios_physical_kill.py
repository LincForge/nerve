"""[contract] tests for physical-iOS bundle PID resolution and kill (linc-nerve-7eb0).

Pins the exact devicectl argv/kwargs, the temp-file lifecycle, the hostile-JSON
handling and the main-before-extension ordering of ``core/ios_apps_logs`` and
the ``mcp/diagnostic_tools`` delegation, so each behaviour has a killing test.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import linc_nerve.core.ios  # noqa: F401  (resolves the ios <-> ios_discovery import cycle)
from linc_nerve.core import engagement, ios_apps_logs
from linc_nerve.core.ios import IOSController
from linc_nerve.core.ios_apps_logs import (
    _bundle_url_prefix,
    _default_run_devicectl,
    _extract_proc_pid,
    _lookup_physical_bundle,
    _pids_for_url_prefix,
    _result_list,
    devicectl_info_json,
    find_physical_bundle_pids,
)
from linc_nerve.core.ios_discovery import XcodeNotReadyError
from linc_nerve.mcp.diagnostic_tools import _devicectl_json, _devicectl_pid

SERIAL = "00008110-0000000000000001"
BUNDLE = "com.example.app"
APP_URL = "/private/var/containers/Bundle/Application/UUID/Example.app/"
INSTALLED = {"result": {"apps": [{"bundleIdentifier": BUNDLE, "url": APP_URL}]}}


def _procs(*entries: tuple[str, int]) -> dict[str, Any]:
    return {
        "result": {
            "runningProcesses": [
                {"executable": f"{APP_URL}{rel}", "processIdentifier": pid} for rel, pid in entries
            ]
        }
    }


class Recorder:
    """Fake devicectl runner: records every call and serves queued JSON payloads by info kind."""

    def __init__(self, **payloads: list[Any]) -> None:
        self.payloads = {kind: list(items) for kind, items in payloads.items()}
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.paths: list[Path] = []

    def __call__(self, *args: Any, **kwargs: Any) -> MagicMock:
        self.calls.append((args, kwargs))
        if "--json-output" in args:
            path = Path(args[args.index("--json-output") + 1])
            assert path.exists(), "temp file must exist when devicectl is invoked"
            self.paths.append(path)
            item = self.payloads[args[2]].pop(0)
            if isinstance(item, BaseException):
                raise item
            path.write_text(item if isinstance(item, str) else json.dumps(item), encoding="utf-8")
        return MagicMock(returncode=0)


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    monkeypatch.setattr(engagement, "scratch_dir", lambda: root)
    return root


class TestDefaultRunDevicectl:
    @pytest.mark.parametrize("check", [True, False])
    def test_runs_xcrun_devicectl_with_exact_kwargs(self, check: bool) -> None:
        sentinel = MagicMock()
        with (
            patch("linc_nerve.core.ios_discovery.xcode_first_launch_ready", return_value=True),
            patch.object(ios_apps_logs.subprocess, "run", return_value=sentinel) as run,
        ):
            assert _default_run_devicectl("device", "info", "apps", check=check) is sentinel
        run.assert_called_once_with(
            ["xcrun", "devicectl", "device", "info", "apps"],
            capture_output=True,
            text=True,
            check=check,
            timeout=15,
        )

    def test_xcode_gate_blocks_before_spawning(self) -> None:
        with (
            patch("linc_nerve.core.ios_discovery.xcode_first_launch_ready", return_value=False),
            patch.object(ios_apps_logs.subprocess, "run") as run,
            pytest.raises(XcodeNotReadyError),
        ):
            _default_run_devicectl("device", "info", "apps", check=True)
        run.assert_not_called()


class TestDevicectlInfoJson:
    def test_forwards_exact_argv_check_true_and_unlinks(self, scratch: Path) -> None:
        rec = Recorder(processes=[{"result": {"runningProcesses": []}}])
        data = devicectl_info_json(SERIAL, "processes", run_devicectl=rec, check=True)
        assert data == {"result": {"runningProcesses": []}}
        [(args, kwargs)] = rec.calls
        [path] = rec.paths
        assert args == (
            "device",
            "info",
            "processes",
            "--device",
            SERIAL,
            "--json-output",
            str(path),
        )
        assert kwargs == {"check": True}
        assert path.parent == scratch
        assert path.name.endswith(".json") and not path.name.endswith("XX.json")
        assert path.suffix == ".json"
        assert not path.exists()
        assert list(scratch.iterdir()) == []

    def test_forwards_check_false(self, scratch: Path) -> None:
        rec = Recorder(apps=[{}])
        assert devicectl_info_json(SERIAL, "apps", run_devicectl=rec, check=False) == {}
        assert rec.calls[0][1] == {"check": False}

    def test_uses_default_runner_when_none_given(self, scratch: Path) -> None:
        rec = Recorder(apps=[{"ok": 1}])
        with patch.object(ios_apps_logs, "_default_run_devicectl", rec):
            assert devicectl_info_json(SERIAL, "apps", check=True) == {"ok": 1}
        assert rec.calls[0][0][:5] == ("device", "info", "apps", "--device", SERIAL)

    def test_non_object_root_raises_exact_message_and_unlinks(self, scratch: Path) -> None:
        rec = Recorder(apps=["[1, 2]"])
        with pytest.raises(ValueError) as exc:
            devicectl_info_json(SERIAL, "apps", run_devicectl=rec, check=True)
        assert str(exc.value) == "devicectl device info apps did not return a JSON object"
        assert list(scratch.iterdir()) == []

    def test_runner_error_propagates_and_unlinks(self, scratch: Path) -> None:
        cpe = subprocess.CalledProcessError(1, ["xcrun", "devicectl"])
        rec = Recorder(apps=[cpe])
        with pytest.raises(subprocess.CalledProcessError):
            devicectl_info_json(SERIAL, "apps", run_devicectl=rec, check=True)
        assert list(scratch.iterdir()) == []

    def test_unlink_oserror_is_suppressed(self, scratch: Path) -> None:
        rec = Recorder(apps=[{"ok": 1}])
        with patch.object(ios_apps_logs.os, "unlink", side_effect=OSError("busy")) as unlink:
            assert devicectl_info_json(SERIAL, "apps", run_devicectl=rec, check=True) == {"ok": 1}
        unlink.assert_called_once_with(str(rec.paths[0]))

    def test_unlink_oserror_does_not_mask_runner_error(self, scratch: Path) -> None:
        def runner(*args: Any, **kwargs: Any) -> None:
            Path(args[args.index("--json-output") + 1]).unlink()
            raise subprocess.CalledProcessError(1, ["xcrun", "devicectl"])

        with pytest.raises(subprocess.CalledProcessError):
            devicectl_info_json(SERIAL, "apps", run_devicectl=runner, check=True)


class TestResultList:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ({}, []),
            ({"result": []}, []),
            ({"result": "x"}, []),
            ({"result": {}}, []),
            ({"result": {"apps": {"a": 1}}}, []),
            ({"result": {"apps": "abc"}}, []),
            ({"result": {"apps": [1, "x"]}}, [1, "x"]),
            ({"result": {"other": [1], "apps": []}}, []),
        ],
    )
    def test_hostile_shapes(self, data: dict[str, Any], expected: list[Any]) -> None:
        assert _result_list(data, "apps") == expected


def _apps(*entries: Any) -> dict[str, Any]:
    return {"result": {"apps": list(entries)}}


class TestBundleUrlPrefix:
    def test_match_after_other_apps_is_found(self) -> None:
        data = _apps(
            "junk",
            {"bundleIdentifier": "com.example.other", "url": "/x/Other.app/"},
            {"bundleIdentifier": BUNDLE, "url": APP_URL},
        )
        assert _bundle_url_prefix(data, BUNDLE) == APP_URL

    def test_empty_url_entry_skipped_for_later_valid_entry(self) -> None:
        data = _apps(
            {"bundleIdentifier": BUNDLE, "url": "  /  "},
            {"bundleIdentifier": BUNDLE, "url": APP_URL},
        )
        assert _bundle_url_prefix(data, BUNDLE) == APP_URL

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("/a/BoX/", "/a/BoX/"),
            ("/a/BoX", "/a/BoX/"),
            ("  /a/Ex.app///  ", "/a/Ex.app/"),
            ("\t/a/Ex.app\n", "/a/Ex.app/"),
            ("", ""),
            ("/", ""),
            ("   ", ""),
            (None, ""),
            (42, ""),
        ],
    )
    def test_url_normalisation(self, url: Any, expected: str) -> None:
        assert _bundle_url_prefix(_apps({"bundleIdentifier": BUNDLE, "url": url}), BUNDLE) == (
            expected
        )

    def test_missing_url_and_unknown_bundle_return_empty(self) -> None:
        assert _bundle_url_prefix(_apps({"bundleIdentifier": BUNDLE}), BUNDLE) == ""
        assert _bundle_url_prefix(_apps({"bundleIdentifier": "com.example.x"}), BUNDLE) == ""
        assert _bundle_url_prefix({}, BUNDLE) == ""


class TestExtractProcPid:
    @pytest.mark.parametrize(
        ("rel", "is_ext"),
        [
            ("Example", False),
            ("PlugIns/Widget/Widget", True),
            ("Widget.appex/Widget", True),
            ("PlugIns/Widget.appex/Widget", True),
            ("plugins/Widget/Widget", False),
            ("Widget.APPEX/Widget", False),
            ("PlugInsX/Widget", False),
        ],
    )
    def test_extension_classification_is_exact_and_case_sensitive(
        self, rel: str, is_ext: bool
    ) -> None:
        seen: set[int] = set()
        proc = {"executable": f"{APP_URL}{rel}", "processIdentifier": 77}
        assert _extract_proc_pid(proc, APP_URL, seen) == (77, is_ext)
        assert seen == {77}

    def test_pid_one_is_valid(self) -> None:
        seen: set[int] = set()
        proc = {"executable": f"{APP_URL}Example", "processIdentifier": 1}
        assert _extract_proc_pid(proc, APP_URL, seen) == (1, False)
        assert seen == {1}

    @pytest.mark.parametrize(
        "proc",
        [
            "not-a-dict",
            None,
            {"executable": f"{APP_URL}Example", "processIdentifier": 0},
            {"executable": f"{APP_URL}Example", "processIdentifier": -5},
            {"executable": f"{APP_URL}Example", "processIdentifier": True},
            {"executable": f"{APP_URL}Example", "processIdentifier": "9"},
            {"executable": f"{APP_URL}Example", "processIdentifier": 9.0},
            {"executable": f"{APP_URL}Example"},
            {"executable": None, "processIdentifier": 9},
            {"executable": "/other/Example.app/Example", "processIdentifier": 9},
            {"executable": f"/x{APP_URL}Example", "processIdentifier": 9},
            {"executable": f"{APP_URL}Example", "processIdentifier": 5},
        ],
    )
    def test_rejected_entries_leave_seen_untouched(self, proc: Any) -> None:
        seen = {5}
        assert _extract_proc_pid(proc, APP_URL, seen) is None
        assert seen == {5}


class TestPidsForUrlPrefix:
    def test_main_before_extensions_dedup_and_filtered(self) -> None:
        data = _procs(
            ("PlugIns/W.appex/W", 101),
            ("Example", 100),
            ("Example", 100),
            ("Share.appex/Share", 102),
            ("Helper", 103),
        )
        data["result"]["runningProcesses"].append(
            {"executable": "/other/App.app/App", "processIdentifier": 104}
        )
        assert _pids_for_url_prefix(data, APP_URL) == [100, 103, 101, 102]

    @pytest.mark.parametrize("data", [{}, {"result": None}, {"result": {"runningProcesses": 3}}])
    def test_hostile_shapes_yield_no_pids(self, data: dict[str, Any]) -> None:
        assert _pids_for_url_prefix(data, APP_URL) == []


class TestLookupPhysicalBundle:
    def test_serial_forwarded_to_apps_and_processes(self, scratch: Path) -> None:
        rec = Recorder(apps=[INSTALLED], processes=[_procs(("Example", 4242))])
        assert _lookup_physical_bundle(SERIAL, BUNDLE, run_devicectl=rec) == (APP_URL, [4242])
        assert [c[0][:5] for c in rec.calls] == [
            ("device", "info", "apps", "--device", SERIAL),
            ("device", "info", "processes", "--device", SERIAL),
        ]
        assert [c[1] for c in rec.calls] == [{"check": True}, {"check": True}]

    def test_not_installed_skips_processes_query(self, scratch: Path) -> None:
        rec = Recorder(apps=[_apps({"bundleIdentifier": "com.example.other", "url": APP_URL})])
        assert _lookup_physical_bundle(SERIAL, BUNDLE, run_devicectl=rec) == ("", [])
        assert len(rec.calls) == 1

    def test_find_physical_bundle_pids_forwards_serial(self, scratch: Path) -> None:
        rec = Recorder(
            apps=[INSTALLED], processes=[_procs(("PlugIns/W.appex/W", 8), ("Example", 7))]
        )
        assert find_physical_bundle_pids(SERIAL, BUNDLE, run_devicectl=rec) == [7, 8]
        assert [c[0][4] for c in rec.calls] == [SERIAL, SERIAL]


class TestKillPhysical:
    def test_exact_call_sequence_terminates_by_pid_then_verifies(self, scratch: Path) -> None:
        ctrl = IOSController()
        rec = Recorder(
            apps=[INSTALLED],
            processes=[_procs(("PlugIns/W.appex/W", 101), ("Example", 100)), _procs()],
        )
        with patch.object(ctrl, "_run_devicectl", side_effect=rec):
            ctrl.kill(SERIAL, BUNDLE)
        calls = [(args[:5] if "--json-output" in args else args, kw) for args, kw in rec.calls]
        assert calls == [
            (("device", "info", "apps", "--device", SERIAL), {"check": True}),
            (("device", "info", "processes", "--device", SERIAL), {"check": True}),
            (
                ("device", "process", "terminate", "--device", SERIAL, "--pid", "100"),
                {"check": False},
            ),
            (
                ("device", "process", "terminate", "--device", SERIAL, "--pid", "101"),
                {"check": False},
            ),
            (("device", "info", "processes", "--device", SERIAL), {"check": True}),
        ]
        assert list(scratch.iterdir()) == []

    def test_surviving_pid_raises_exact_message(self, scratch: Path) -> None:
        ctrl = IOSController()
        rec = Recorder(
            apps=[INSTALLED],
            processes=[_procs(("Example", 100)), _procs(("Example", 100))],
        )
        with (
            patch.object(ctrl, "_run_devicectl", side_effect=rec),
            pytest.raises(RuntimeError) as e,
        ):
            ctrl.kill(SERIAL, BUNDLE)
        assert str(e.value) == f"Failed to terminate {BUNDLE} on {SERIAL}; remaining PIDs: [100]"

    def test_verification_scoped_to_bundle_url(self, scratch: Path) -> None:
        ctrl = IOSController()
        other = {
            "result": {
                "runningProcesses": [
                    {"executable": "/private/var/Other.app/Other", "processIdentifier": 100}
                ]
            }
        }
        rec = Recorder(apps=[INSTALLED], processes=[_procs(("Example", 100)), other])
        with patch.object(ctrl, "_run_devicectl", side_effect=rec):
            ctrl.kill(SERIAL, BUNDLE)
        assert len(rec.calls) == 4

    def test_installed_but_not_running_is_noop_without_verify(self, scratch: Path) -> None:
        ctrl = IOSController()
        rec = Recorder(apps=[INSTALLED], processes=[_procs()])
        with patch.object(ctrl, "_run_devicectl", side_effect=rec):
            ctrl.kill(SERIAL, BUNDLE)
        assert [c[0][2] for c in rec.calls] == ["apps", "processes"]


class TestListAppsPhysical:
    def test_exact_call_check_false_and_filtering(self, scratch: Path) -> None:
        ctrl = IOSController()
        rec = Recorder(
            apps=[
                _apps(
                    {"bundleIdentifier": "com.example.b"},
                    "junk",
                    {"name": "no-bundle-id"},
                    {"bundleIdentifier": ""},
                    {"bundleIdentifier": None},
                    {"bundleIdentifier": "com.example.a"},
                    {"bundleIdentifier": "com.example.a"},
                )
            ]
        )
        with patch.object(ctrl, "_run_devicectl", side_effect=rec):
            assert ctrl._list_apps_physical(SERIAL) == ["com.example.a", "com.example.b"]
        [(args, kwargs)] = rec.calls
        assert args == (
            "device",
            "info",
            "apps",
            "--device",
            SERIAL,
            "--json-output",
            str(rec.paths[0]),
        )
        assert kwargs == {"check": False}
        assert list(scratch.iterdir()) == []

    @pytest.mark.parametrize(
        "payload",
        ["{bad json", "[]", "", {}, {"result": []}, {"result": {"apps": None}}],
    )
    def test_unparseable_or_hostile_payload_returns_empty(
        self, scratch: Path, payload: Any
    ) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run_devicectl", side_effect=Recorder(apps=[payload])):
            assert ctrl._list_apps_physical(SERIAL) == []

    def test_missing_output_file_returns_empty(self, scratch: Path) -> None:
        ctrl = IOSController()

        def runner(*args: Any, **kwargs: Any) -> None:
            Path(args[args.index("--json-output") + 1]).unlink()

        with patch.object(ctrl, "_run_devicectl", side_effect=runner):
            assert ctrl._list_apps_physical(SERIAL) == []

    def test_xcode_gate_error_propagates(self, scratch: Path) -> None:
        ctrl = IOSController()
        with (
            patch.object(ctrl, "_run_devicectl", side_effect=XcodeNotReadyError()),
            pytest.raises(XcodeNotReadyError),
        ):
            ctrl._list_apps_physical(SERIAL)


class TestDiagnosticToolsDelegation:
    def test_devicectl_json_forwards_serial_and_info(self) -> None:
        with patch.object(ios_apps_logs, "devicectl_info_json", return_value={"k": 1}) as m:
            assert _devicectl_json(SERIAL, "processes") == {"k": 1}
        m.assert_called_once_with(SERIAL, "processes", check=True)

    @pytest.mark.parametrize(("pids", "expected"), [([7, 8], 7), ([1], 1), ([], None)])
    def test_devicectl_pid_returns_first_pid(self, pids: list[int], expected: int | None) -> None:
        with patch.object(ios_apps_logs, "find_physical_bundle_pids", return_value=pids) as m:
            assert _devicectl_pid(SERIAL, BUNDLE) == expected
        m.assert_called_once_with(SERIAL, BUNDLE)

    def test_devicectl_pid_swallows_errors(self) -> None:
        with patch.object(
            ios_apps_logs, "find_physical_bundle_pids", side_effect=RuntimeError("x")
        ):
            assert _devicectl_pid(SERIAL, BUNDLE) is None
