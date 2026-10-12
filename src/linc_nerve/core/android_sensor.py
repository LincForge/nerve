"""Android sensor injection + screen recording — emulator console + screenrecord.

Split from android.py (#53) as a mixin to keep both files under the hot-file LOC
ceiling. References to test-patched / android.py module-globals (``subprocess``,
``time``, ``_socket_mod``, ``logger``, ``ADB_PATH``, ``os``, ``contextlib``,
``Path``) are routed through the parent module (``_android.<name>``) so patches
on ``linc_nerve.core.android.*`` keep working after the move.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import android as _android
from .emulator_console import auth_line, sensor_set_line
from .recording import assert_recording_nonempty, assert_recording_playable

#: Android's guest-side ``screenrecord`` self-terminates at this cap with no
#: signal to the caller — see ``_truncation_info``/``recording_info`` below
#: (linc-nerve-zqy.3).
SCREENRECORD_CAP_S = 180


def _clock() -> float:
    """Monotonic clock seam — patched in tests to control elapsed-time math."""
    return _android.time.monotonic()


def _truncation_info(started_at: float, stopped_at: float) -> dict[str, Any]:
    """Pure elapsed/truncation computation for a GUEST recording.

    Kept out of ``stop_recording`` so extracting it doesn't raise that
    function's CRAP score (linc-nerve-zqy.3).
    """
    elapsed = stopped_at - started_at
    info: dict[str, Any] = {
        "elapsed_s": round(elapsed, 1),
        "truncated": elapsed > SCREENRECORD_CAP_S,
    }
    if info["truncated"]:
        info["capped_at_s"] = SCREENRECORD_CAP_S
    return info


class _SensorRecordMixin:
    """Sensor injection + screen recording methods for AndroidController."""

    @staticmethod
    def _emulator_console_port(serial: str) -> int:
        """Extract the telnet console port from an emulator serial.

        E.g. "emulator-5554" -> 5554.
        Raises ValueError if the serial is not an emulator serial.
        """
        if not serial.startswith("emulator-"):
            raise ValueError(f"'{serial}' is not an emulator serial")
        try:
            return int(serial.split("-", 1)[1])
        except (IndexError, ValueError) as exc:
            raise ValueError(f"Cannot parse console port from '{serial}'") from exc

    @staticmethod
    def _read_auth_token() -> str:
        """Read the emulator console auth token from ~/.emulator_console_auth_token."""
        token_path = _android.os.path.expanduser("~/.emulator_console_auth_token")
        try:
            with open(token_path) as f:
                return f.read().strip()
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Emulator auth token not found at {token_path}. "
                "Is the Android emulator installed?"
            ) from None

    def inject_sensor(
        self,
        serial: str,
        sensor_type: str,
        x: float,
        y: float,
        z: float,
    ) -> dict[str, Any]:
        """Inject sensor data into an Android emulator via its telnet console.

        Connects to the emulator's telnet console, authenticates, and sends
        a sensor set command.

        Args:
            serial: Emulator serial (e.g. "emulator-5554").
            sensor_type: Sensor name (acceleration, gyroscope, magnetic-field, etc.).
            x: X-axis value.
            y: Y-axis value.
            z: Z-axis value.

        Returns:
            Dict with success status or error message.

        Raises:
            ValueError: ``sensor_type`` is not an allowlisted emulator sensor
                name, an axis value is not a number, or the auth token holds a
                line break — validated before any console I/O so no second
                console verb can be injected (security review M1).
        """
        port = self._emulator_console_port(serial)
        sensor_cmd = sensor_set_line(sensor_type, x, y, z)
        auth_cmd = auth_line(self._read_auth_token())

        sock = _android._socket_mod.socket(
            _android._socket_mod.AF_INET, _android._socket_mod.SOCK_STREAM
        )
        sock.settimeout(5)
        try:
            sock.connect(("localhost", port))
        except (ConnectionRefusedError, OSError) as exc:
            sock.close()
            return {
                "error": f"Cannot connect to emulator console on port {port}: {exc}",
                "device": serial,
            }

        try:
            # Helper to receive until a marker or timeout
            def _recv_until(marker: bytes, bufsize: int = 4096) -> str:
                data = b""
                while marker not in data:
                    try:
                        chunk = sock.recv(bufsize)
                    except TimeoutError:
                        break
                    if not chunk:
                        break
                    data += chunk
                return data.decode(errors="replace")

            # Read initial banner
            _recv_until(b"OK")

            # Authenticate
            sock.sendall(auth_cmd)
            auth_resp = _recv_until(b"OK")
            if "OK" not in auth_resp:
                return {
                    "error": "Emulator console authentication failed. "
                    "Check ~/.emulator_console_auth_token.",
                    "device": serial,
                }

            # Send sensor command
            sock.sendall(sensor_cmd)
            resp = _recv_until(b"\n")

            if "OK" in resp:
                return {
                    "success": True,
                    "device": serial,
                    "sensor": sensor_type,
                    "values": {"x": x, "y": y, "z": z},
                }
            else:
                clean = resp.strip()
                return {
                    "error": "sensor set failed: " + clean,
                    "device": serial,
                }
        finally:
            with _android.contextlib.suppress(Exception):
                sock.sendall(b"quit\n")
            with _android.contextlib.suppress(Exception):
                sock.close()

    def _require_ready(self, serial: str) -> None:
        """Raise if the device isn't ready to capture — recording on a display-off
        device yields a 0-byte MP4 (f33c9fc3). Extracted so start_recording stays flat."""
        status = self.ensure_ready(serial)
        if not status.get("ready"):
            raise RuntimeError(
                f"Cannot record: device {serial} not ready ({status.get('reason')}). "
                "Recording on a display-off device yields a 0-byte MP4 (f33c9fc3)."
            )

    def start_recording(self, serial: str) -> None:
        """Start screen recording on the device.

        Emulator serials (``emulator-<port>``) route to the host-side console
        recorder (:meth:`_start_emulator_recording`) — guest-side ``adb shell
        screenrecord`` cannot init an AVC encoder on our AVDs (``Encoder failed
        (err=-38)``, fe7fbb9d). Physical devices keep the guest-side path.
        """
        if serial in self._recording_processes:
            return

        self._require_ready(serial)

        if serial.startswith("emulator-"):
            self._start_emulator_recording(serial)
            return

        cmd = [
            _android.ADB_PATH,
            "-s",
            serial,
            "shell",
            "screenrecord",
            f"/sdcard/nerve_record_{serial}.mp4",
        ]
        process = _android.subprocess.Popen(
            cmd, stdout=_android.subprocess.PIPE, stderr=_android.subprocess.PIPE, text=True
        )
        self._recording_processes[serial] = {
            "target": "guest",
            "process": process,
            "started_at": _clock(),
        }
        _android.time.sleep(1.0)

    def _start_emulator_recording(self, serial: str) -> None:
        """Start the emulator's host-side console recorder.

        ``adb -s <serial> emu screenrecord start <host_path>`` writes directly to
        a HOST path (``.webm``) — there is no guest-side artifact to pull, unlike
        ``shell screenrecord``. The command returns quickly (it just tells the
        emulator console to begin); the actual capture continues in the
        background until ``emu screenrecord stop`` (see ``stop_recording``).
        """
        out_dir = _android.Path("~/.linc/nerve/recordings").expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(_android.time.time())
        host_path = out_dir / f"android_{serial}_{timestamp}.webm"

        cmd = [
            _android.ADB_PATH,
            "-s",
            serial,
            "emu",
            "screenrecord",
            "start",
            str(host_path),
        ]
        result = _android.subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self._recording_processes[serial] = {
            "target": "emulator",
            "host_path": str(host_path),
            "start_stderr": result.stderr or "",
        }
        _android.time.sleep(1.0)

    def stop_recording(self, serial: str) -> str:
        """Stop screen recording and return the local artifact path.

        Raises:
            ValueError: If no recording is active on ``serial``.
            RuntimeError: If the produced artifact is 0 bytes — an encoder
                failure (``Encoder failed (err=-38)``, reproduced on our AVDs)
                otherwise returns success with an empty file (f33c9fc3-adjacent,
                but distinct: that fix guards display-off *before* recording;
                this guards the *artifact* after).
        """
        if serial not in self._recording_processes:
            raise ValueError(f"Not recording on {serial}")

        entry = self._recording_processes.pop(serial)

        if entry.get("target") == "emulator":
            local_path = self._stop_emulator_recording(serial, entry)
        else:
            local_path = self._stop_guest_recording(serial, entry)
            self._record_recording_info(serial, entry)

        return str(local_path)

    def _record_recording_info(self, serial: str, entry: dict[str, Any]) -> None:
        """Store truncation-visibility info for a completed GUEST recording.

        Emulator host-side recordings never reach here (see ``stop_recording``)
        — their console recorder has separate, unverified timing semantics,
        out of scope for this bead. A guest entry with no ``started_at``
        (pre-existing tests that construct ``_recording_processes`` by hand)
        is likewise skipped rather than crashing.
        """
        started_at = entry.get("started_at")
        if started_at is None:
            return
        if not hasattr(self, "_last_recording_info"):
            self._last_recording_info: dict[str, dict[str, Any]] = {}
        self._last_recording_info[serial] = _truncation_info(started_at, _clock())

    def recording_info(self, serial: str) -> dict[str, Any] | None:
        """Truncation-visibility info for the last completed GUEST recording.

        Returns ``None`` for a serial that never had a guest-side recording
        stopped (never recorded, or an emulator host-side recording — those
        store nothing, out of scope here).
        """
        return getattr(self, "_last_recording_info", {}).get(serial)

    def _stop_emulator_recording(self, serial: str, entry: dict[str, Any]) -> Any:
        """Tell the emulator console to finalize the recording, then verify
        the host artifact it already wrote directly (no pull needed)."""
        result = _android.subprocess.run(
            [_android.ADB_PATH, "-s", serial, "emu", "screenrecord", "stop"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        _android.time.sleep(1.0)  # let the emulator flush/finalize the file

        host_path = _android.Path(entry["host_path"])
        stderr = (entry.get("start_stderr") or "") + (result.stderr or "")
        self._assert_recording_nonempty(host_path, stderr)
        return host_path

    def _stop_guest_recording(self, serial: str, entry: dict[str, Any]) -> Any:
        """Terminate the guest-side screenrecord process, pull the artifact, and
        clean up the remote copy.

        Drains stdout/stderr via ``communicate()`` instead of ``wait()`` followed
        by a later ``stderr.read()``. screenrecord's pipe buffer is bounded (~64KB
        on macOS); on a long recording that fills before we terminate, the child
        blocks on the write, ``wait()`` times out, we ``kill()`` it, and a plain
        post-wait ``stderr.read()`` never runs — losing both the recording and
        the encoder diagnostic this guard exists to surface. ``communicate()``
        drains both pipes while waiting, so it can't deadlock on a full buffer.
        """
        process = entry["process"]
        process.terminate()
        try:
            _, stderr = process.communicate(timeout=10)
        except _android.subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate(timeout=5)
        stderr = stderr or ""

        _android.time.sleep(1.0)

        out_dir = _android.Path("~/.linc/nerve/recordings").expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(_android.time.time())
        local_path = out_dir / f"android_{serial}_{timestamp}.mp4"

        remote_path = f"/sdcard/nerve_record_{serial}.mp4"
        # Bounded so a stalled USB transfer can't pin the daemon's tool worker
        # (pull moves a potentially large mp4, rm is a quick shell op).
        _android.subprocess.run(
            [_android.ADB_PATH, "-s", serial, "pull", remote_path, str(local_path)],
            check=True,
            timeout=60,
        )
        _android.subprocess.run(
            [_android.ADB_PATH, "-s", serial, "shell", "rm", remote_path],
            check=True,
            timeout=15,
        )

        self._assert_recording_nonempty(local_path, stderr)
        assert_recording_playable(local_path, stderr)
        return local_path

    @staticmethod
    def _assert_recording_nonempty(path: Any, stderr: str) -> None:
        """Raise if the recorded artifact is 0 bytes — a screenrecord encoder
        failure (``Encoder failed (err=-38)``, reproduced on our AVDs) otherwise
        returns success with an empty file unless checked explicitly."""
        assert_recording_nonempty(Path(path), stderr)
