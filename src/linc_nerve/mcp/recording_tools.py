import time
from typing import Any

from linc_nerve.core import action_log, frames
from linc_nerve.mcp import _core
from linc_nerve.mcp._core import device_tool, global_tool


def get_device_sync(device: str) -> Any:
    dev, platform = _core._resolve_device(device)
    if dev is None:
        raise ValueError(f"Device not found: {platform}")
    return _core._get_controller(platform)


def _recording_extras(dev: Any, device: str) -> dict[str, Any]:
    """Merge Android truncation-visibility info into a recording result.

    ``recording_info`` only exists on the Android controller, and only for
    guest-target recordings — a bare MagicMock device (existing tests) or
    the iOS controller (no such attribute) must yield no extra keys, so a
    non-dict return is treated as "nothing to add".
    """
    fn = getattr(dev, "recording_info", None)
    info = fn(device) if callable(fn) else None
    if not isinstance(info, dict):
        return {}
    extras = dict(info)
    if extras.get("truncated"):
        extras["warning"] = "Android screenrecord stops at 180s; the recording is truncated"
    return extras


@device_tool
def _nerve_record_start_sync(device: str) -> dict[str, Any]:
    """Begin recording video to disk — open-ended MP4 capture on the device.

    USE FOR: open-ended MP4 capture, a durable on-disk recording artifact
        that outlives the session, user-flow walkthrough videos.
    NOT FOR: an in-memory frame source (nerve_stream_start — no MP4); a
        fixed known-duration clip (nerve_record_fixed); a single PNG
        snapshot (nerve_screenshot).
    TIP: must be paired with nerve_record_stop to flush the MP4; one
        recording per device at a time. Android and iOS.

    Args:
        device: Device serial (Android or iOS).

    Returns:
        dict with status="success" and a message to call nerve_record_stop
        when finished — or error.
    """
    dev = get_device_sync(device)
    dev.start_recording(device)
    action_log.REGISTRY.mark_recording_started(device, action_log._clock())
    return {
        "status": "success",
        "message": "Recording started. Call nerve_record_stop when finished.",
    }


@device_tool
def _nerve_record_stop_sync(device: str) -> dict[str, Any]:
    """Finalise the MP4 file and end the durable recording session.

    USE FOR: finalising the MP4 when a recording started by
        nerve_record_start is done, pulling the artifact to the host,
        returning the saved file path.
    NOT FOR: pausing — no pause exists; to start fresh, call
        nerve_record_start again. Tearing down a live mirror session
        (nerve_stream_stop — in-memory stream, not an MP4).
    TIP: blocks until the trailer flushes and the file transfers; latency
        scales with recording length. Errors if no recording is active.

    Args:
        device: Device serial (Android or iOS).

    Returns:
        dict with status="success" and file path (host MP4) — or error.
    """
    dev = get_device_sync(device)
    path = dev.stop_recording(device)
    action_log.REGISTRY.mark_recording_stopped(device)
    return {"status": "success", "file_path": path, **_recording_extras(dev, device)}


@device_tool
def _nerve_record_fixed_sync(device: str, duration_seconds: int = 15) -> dict[str, Any]:
    """Record a fixed-length screen video — self-terminating MP4 capture.

    Equivalent to nerve_record_start + sleep + nerve_record_stop in one
    blocking call.

    USE FOR: time-boxed MP4 recording of a known duration, fixed-length
        walkthrough capture, self-terminating single-call recording.
    NOT FOR: open-ended capture of unknown duration (nerve_record_start +
        nerve_record_stop); a live mirror session (nerve_stream_start — no
        MP4); a single PNG snapshot (nerve_screenshot).
    TIP: blocks for duration_seconds plus finalise/transfer time; not while
        another recording is active on the device.

    Args:
        device: Device serial (Android or iOS).
        duration_seconds: How long to record before auto-stopping (default 15).

    Returns:
        dict with status="success" and file_path (host MP4) — or error.
    """
    dev = get_device_sync(device)
    dev.start_recording(device)
    action_log.REGISTRY.mark_recording_started(device, action_log._clock())
    time.sleep(duration_seconds)
    path = dev.stop_recording(device)
    action_log.REGISTRY.mark_recording_stopped(device)
    return {"status": "success", "file_path": path, **_recording_extras(dev, device)}


@global_tool
def _nerve_extract_frames_sync(
    video_path: str, timestamps: str, out_dir: str = ""
) -> dict[str, Any]:
    """Extract PNG frames at given timestamps from a host-side MP4 recording.

    Seeks to the keyframe before each timestamp and decodes forward to the
    first frame at or after it (PyAV, no ffmpeg binary, no device needed).
    Each frame reports mean luma and a ``black`` flag — FLAG_SECURE apps
    record as black frames, so a black frame is not evidence of the bug.

    USE FOR: pulling still frames out of a finished recording (from
        nerve_record_stop / nerve_record_fixed) to attach as bug-report
        evidence, checking what the screen showed at a given second,
        detecting a blanked (FLAG_SECURE) recording.
    NOT FOR: capturing the live screen (nerve_screenshot / nerve_state);
        recording video (nerve_record_start / nerve_record_fixed).
    TIP: timestamps outside 0..duration are rejected with an error that
        names them — never clamped — and nothing is written. Duplicate
        timestamps (same millisecond) share one file. Paths are on the host
        running nerve (the daemon's filesystem when called over :8111).

    Args:
        video_path: Host path of the MP4 recording — must end in .mp4/.mov;
            always demuxed as MP4 (never sniffed, so playlists are refused).
        timestamps: Comma-separated seconds, e.g. "0.5,12,47.25", each in
            0 <= t < duration (t == duration is out of range).
        out_dir: Output directory for ``frame_<ms>.png`` files (default:
            ``<video dir>/<video stem>_frames``). Must resolve inside the
            video's directory, $LINC_HOME or the system temp dir.

    Returns:
        dict with duration_s, frames (list of {t_s, t_actual_s, path, width,
        height, mean_luma, black}) and black_frames (count) — or an error
        dict with validation_error=True (and out_of_range + duration_s when
        timestamps fall outside the video).
    """
    return frames.extract_request(video_path, timestamps, out_dir)
