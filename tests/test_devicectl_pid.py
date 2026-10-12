"""Characterization tests for _devicectl_pid in diagnostic_tools.py.

Covers all branches of the two-step devicectl PID lookup:
Step 1: Get app URL from device apps list JSON
Step 2: Match app URL against running process executables
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.mcp.diagnostic_tools import _devicectl_pid

SERIAL = "00008110-0000000000000001"
BUNDLE = "com.lincmobile.sniperpulse"


def _write_json_to_path(data: dict, path: str) -> None:
    """Helper: write JSON data to a file path."""
    with open(path, "w") as f:
        json.dump(data, f)


class TestDevicectlPid:
    """Characterization tests for _devicectl_pid."""

    def test_bundle_found_and_running(self, tmp_path: pytest.TempPathFactory) -> None:
        """Happy path: bundle found in apps list, matching process found -> returns PID."""
        apps_file = tmp_path / "apps.json"
        procs_file = tmp_path / "procs.json"

        apps_data = {
            "result": {
                "apps": [
                    {
                        "bundleIdentifier": BUNDLE,
                        "url": "file:///private/var/containers/Bundle/App/ABC/iosApp.app/",
                    },
                    {
                        "bundleIdentifier": "com.other.app",
                        "url": "file:///private/var/containers/Bundle/App/DEF/other.app/",
                    },
                ]
            }
        }
        procs_data = {
            "result": {
                "runningProcesses": [
                    {
                        "executable": "file:///private/var/containers/Bundle/App/ABC/iosApp.app/iosApp",
                        "processIdentifier": 12345,
                    },
                    {
                        "executable": "file:///usr/bin/something",
                        "processIdentifier": 999,
                    },
                ]
            }
        }

        # Track which tempfile we're on
        call_count = 0
        paths = [str(apps_file), str(procs_file)]

        class FakeTempFile:
            def __init__(self, **kwargs):
                nonlocal call_count
                self.name = paths[call_count]
                call_count += 1

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def mock_subprocess_run(cmd, **kwargs):
            """Write JSON to the output path that devicectl would write."""
            # Find --json-output arg and the path after it
            if "--json-output" in cmd:
                idx = cmd.index("--json-output")
                output_path = cmd[idx + 1]
                if "apps" in cmd:
                    _write_json_to_path(apps_data, output_path)
                elif "processes" in cmd:
                    _write_json_to_path(procs_data, output_path)
            return MagicMock(returncode=0)

        with (
            patch("tempfile.NamedTemporaryFile", FakeTempFile),
            patch("subprocess.run", side_effect=mock_subprocess_run),
        ):
            result = _devicectl_pid(SERIAL, BUNDLE)

        assert result == 12345

    def test_bundle_not_found(self, tmp_path: pytest.TempPathFactory) -> None:
        """Bundle ID not in apps list -> app_url is None -> returns None."""
        apps_file = tmp_path / "apps.json"

        apps_data = {
            "result": {
                "apps": [
                    {
                        "bundleIdentifier": "com.other.app",
                        "url": "file:///some/path/",
                    },
                ]
            }
        }

        class FakeTempFile:
            def __init__(self, **kwargs):
                self.name = str(apps_file)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def mock_subprocess_run(cmd, **kwargs):
            if "--json-output" in cmd:
                idx = cmd.index("--json-output")
                _write_json_to_path(apps_data, cmd[idx + 1])
            return MagicMock(returncode=0)

        with (
            patch("tempfile.NamedTemporaryFile", FakeTempFile),
            patch("subprocess.run", side_effect=mock_subprocess_run),
        ):
            result = _devicectl_pid(SERIAL, BUNDLE)

        assert result is None

    def test_apps_json_exception(self) -> None:
        """Exception during step 1 (subprocess.run fails) -> returns None."""

        class FakeTempFile:
            def __init__(self, **kwargs):
                self.name = "/tmp/nonexistent_apps.json"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        with (
            patch("tempfile.NamedTemporaryFile", FakeTempFile),
            patch("subprocess.run", side_effect=OSError("xcrun not found")),
        ):
            result = _devicectl_pid(SERIAL, BUNDLE)

        assert result is None

    def test_no_matching_process(self, tmp_path: pytest.TempPathFactory) -> None:
        """App URL found but no running process matches the URL prefix -> returns None."""
        apps_file = tmp_path / "apps.json"
        procs_file = tmp_path / "procs.json"

        apps_data = {
            "result": {
                "apps": [
                    {
                        "bundleIdentifier": BUNDLE,
                        "url": "file:///private/var/containers/Bundle/App/ABC/iosApp.app/",
                    },
                ]
            }
        }
        procs_data = {
            "result": {
                "runningProcesses": [
                    {
                        "executable": "file:///private/var/containers/Bundle/App/OTHER/otherApp.app/otherApp",
                        "processIdentifier": 999,
                    },
                ]
            }
        }

        call_count = 0
        paths = [str(apps_file), str(procs_file)]

        class FakeTempFile:
            def __init__(self, **kwargs):
                nonlocal call_count
                self.name = paths[call_count]
                call_count += 1

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def mock_subprocess_run(cmd, **kwargs):
            if "--json-output" in cmd:
                idx = cmd.index("--json-output")
                output_path = cmd[idx + 1]
                if "apps" in cmd:
                    _write_json_to_path(apps_data, output_path)
                elif "processes" in cmd:
                    _write_json_to_path(procs_data, output_path)
            return MagicMock(returncode=0)

        with (
            patch("tempfile.NamedTemporaryFile", FakeTempFile),
            patch("subprocess.run", side_effect=mock_subprocess_run),
        ):
            result = _devicectl_pid(SERIAL, BUNDLE)

        assert result is None

    def test_process_json_exception(self, tmp_path: pytest.TempPathFactory) -> None:
        """Step 1 succeeds (app found), but step 2 JSON parse fails -> returns None."""
        apps_file = tmp_path / "apps.json"
        procs_file = tmp_path / "procs.json"

        apps_data = {
            "result": {
                "apps": [
                    {
                        "bundleIdentifier": BUNDLE,
                        "url": "file:///private/var/containers/Bundle/App/ABC/iosApp.app/",
                    },
                ]
            }
        }

        call_count = 0
        paths = [str(apps_file), str(procs_file)]

        class FakeTempFile:
            def __init__(self, **kwargs):
                nonlocal call_count
                self.name = paths[call_count]
                call_count += 1

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def mock_subprocess_run(cmd, **kwargs):
            if "--json-output" in cmd:
                idx = cmd.index("--json-output")
                output_path = cmd[idx + 1]
                if "apps" in cmd:
                    _write_json_to_path(apps_data, output_path)
                elif "processes" in cmd:
                    # Write invalid JSON to trigger JSONDecodeError
                    with open(output_path, "w") as f:
                        f.write("{invalid json")
            return MagicMock(returncode=0)

        with (
            patch("tempfile.NamedTemporaryFile", FakeTempFile),
            patch("subprocess.run", side_effect=mock_subprocess_run),
        ):
            result = _devicectl_pid(SERIAL, BUNDLE)

        assert result is None

    def test_empty_apps_list(self, tmp_path: pytest.TempPathFactory) -> None:
        """Empty apps list -> app_url stays None -> returns None."""
        apps_file = tmp_path / "apps.json"

        class FakeTempFile:
            def __init__(self, **kwargs):
                self.name = str(apps_file)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def mock_subprocess_run(cmd, **kwargs):
            if "--json-output" in cmd:
                idx = cmd.index("--json-output")
                _write_json_to_path({"result": {"apps": []}}, cmd[idx + 1])
            return MagicMock(returncode=0)

        with (
            patch("tempfile.NamedTemporaryFile", FakeTempFile),
            patch("subprocess.run", side_effect=mock_subprocess_run),
        ):
            result = _devicectl_pid(SERIAL, BUNDLE)

        assert result is None

    def test_app_url_empty_string(self, tmp_path: pytest.TempPathFactory) -> None:
        """Bundle found but url is empty string -> app_url is falsy -> returns None."""
        apps_file = tmp_path / "apps.json"

        apps_data = {
            "result": {
                "apps": [
                    {
                        "bundleIdentifier": BUNDLE,
                        "url": "",
                    },
                ]
            }
        }

        class FakeTempFile:
            def __init__(self, **kwargs):
                self.name = str(apps_file)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def mock_subprocess_run(cmd, **kwargs):
            if "--json-output" in cmd:
                idx = cmd.index("--json-output")
                _write_json_to_path(apps_data, cmd[idx + 1])
            return MagicMock(returncode=0)

        with (
            patch("tempfile.NamedTemporaryFile", FakeTempFile),
            patch("subprocess.run", side_effect=mock_subprocess_run),
        ):
            result = _devicectl_pid(SERIAL, BUNDLE)

        assert result is None
