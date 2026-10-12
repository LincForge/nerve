"""Frame extraction from a host-side MP4 — PNG stills at given timestamps.

Bead linc-nerve-5l2 (epic linc-nerve-c9i, bug-hunt §6/§7.9). A bug-hunt
agent proposes "the bug is visible at t=…" timestamps against a recording;
this module cuts those frames out with PyAV (already a dependency — no
ffmpeg binary) and flags frames that came out black, which is what a
FLAG_SECURE app produces in a screen recording.

Model-proposed timestamps can be invented (the 053 pilot asked for t=209 s
on a 148 s video), so out-of-range timestamps are rejected, never clamped.
The valid range is half-open, ``0 <= t < duration``.

Inputs arrive from agents and over the :8111 daemon, so they are treated as
untrusted: the file must be named ``.mp4``/``.mov`` and is always opened with
the MP4 demuxer pinned — FFmpeg never sniffs the container, so a playlist
(HLS, ffconcat) disguised as ``.mp4`` cannot make it open the files it
references. ``out_dir`` must resolve inside the video's directory,
``$LINC_HOME`` or the system temp dir.

Pure helpers (``is_black``, ``duration_from``, ``parse_timestamps``,
``out_of_range``, ``unique_by_ms``, ``first_frame_at_or_after``) carry the
logic; the PyAV-touching functions are thin.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Iterable
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import structlog
from PIL import Image, ImageStat

from linc_nerve.core.observability import linc_home

log = structlog.get_logger(__name__)

# A frame is "black" when it is both dark AND flat — a dark-but-textured
# frame (night-mode UI, a dark photo) is real content, not a blanked capture.
BLACK_MEAN_MAX = 8.0
BLACK_STD_MAX = 4.0

# Only MP4/QuickTime recordings are accepted, and they are always demuxed as
# such — never sniffed (FFmpeg's "mp4" input format is the mov,mp4 demuxer).
VIDEO_SUFFIXES = (".mp4", ".mov")
DEMUXER = "mp4"

# Float slack when comparing a decoded pts (exact rational) to a requested t.
_EPS = 1e-6

# Untrusted-input caps: how many timestamps one call may ask for, and the
# largest frame (width x height, 8K UHD) decoded — a crafted file declaring a
# huge frame would otherwise allocate it per timestamp (decompression bomb).
MAX_TIMESTAMPS = 500
MAX_FRAME_PIXELS = 7680 * 4320


def is_black(mean: float, std: float) -> bool:
    """True when a frame's luma is dark (mean < 8) and flat (std-dev < 4)."""
    return mean < BLACK_MEAN_MAX and std < BLACK_STD_MAX


def duration_from(
    container_duration: int | None,
    stream_duration: int | None,
    time_base: Fraction | None,
) -> float:
    """Video duration in seconds: container duration, else stream duration * time_base.

    ``container_duration`` is in ``av.time_base`` units (microseconds).
    """
    if container_duration:
        return container_duration / av.time_base
    if stream_duration and time_base:
        return float(stream_duration * time_base)
    raise ValueError("video duration unknown: neither container nor stream reports it")


def parse_timestamps(text: str) -> list[float]:
    """Parse a comma-separated list of seconds ("1.0, 4.2") into floats."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise ValueError("no timestamps given: pass a comma-separated list of seconds")
    if len(parts) > MAX_TIMESTAMPS:
        raise ValueError(
            f"{len(parts)} timestamps given; pass at most {MAX_TIMESTAMPS} timestamps"
        )
    return [_parse_timestamp(part) for part in parts]


def _parse_timestamp(part: str) -> float:
    """One finite timestamp in seconds, or ``ValueError`` naming *part*."""
    try:
        value = float(part)
    except ValueError:
        raise ValueError(f"not a timestamp in seconds: {part!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"not a finite timestamp: {part!r}")
    return value


def out_of_range(timestamps: Iterable[float], duration: float) -> list[float]:
    """Timestamps outside ``0 <= t < duration`` — rejected, never clamped.

    ``t == duration`` is out of range: no frame starts there.
    """
    return [t for t in timestamps if t < 0 or t >= duration]


class OutOfRangeError(ValueError):
    """Some timestamps fall outside the video; carries them for the error dict."""

    def __init__(self, bad: list[float], duration: float) -> None:
        super().__init__(
            f"timestamps outside the video (0 <= t < {duration:.3f}s), not clamped: "
            f"{format_seconds(bad)}"
        )
        self.extra = {"out_of_range": bad, "duration_s": round(duration, 3)}


def format_seconds(values: Iterable[float]) -> str:
    """Render seconds compactly for an error message ("4.2, 209")."""
    return ", ".join(f"{v:g}" for v in values)


def unique_by_ms(timestamps: Iterable[float]) -> list[tuple[int, float]]:
    """Sorted ``(ms, t)`` pairs, one per millisecond — duplicates share one file."""
    seen: dict[int, float] = {}
    for t in timestamps:
        seen.setdefault(round(t * 1000), t)
    return sorted(seen.items(), key=lambda item: item[1])


def first_frame_at_or_after(frames: Iterable[Any], target_pts: float) -> Any:
    """First frame with ``pts >= target_pts``, else the last frame seen.

    The fallback covers a timestamp inside the last frame's display interval
    (after the last frame starts, before ``duration``): no frame starts at or
    after it, so the frame on screen at that moment — the last one — is
    returned, and its pts is *below* the target. Frames without a pts are
    skipped.
    """
    last = None
    for frame in frames:
        if frame.pts is None:
            continue
        if frame.pts >= target_pts - _EPS:
            return frame
        last = frame
    if last is None:
        raise ValueError("no decodable video frame at or before the requested timestamp")
    return last


def luma_stats(image: Image.Image) -> tuple[float, float]:
    """Mean and standard deviation of the image's luma (0-255)."""
    stat = ImageStat.Stat(image.convert("L"))
    return stat.mean[0], stat.stddev[0]


def default_out_dir(video: Path) -> Path:
    """``<video dir>/<video stem>_frames`` — used when the caller names no out_dir."""
    return video.parent / f"{video.stem}_frames"


def _system_tmp() -> Path:
    """The system temp dir (seam: tests re-point it)."""
    return Path(tempfile.gettempdir())


def allowed_out_roots(video: Path) -> list[Path]:
    """Directories an out_dir may resolve into: the video's dir, $LINC_HOME, system tmp."""
    video_dir = video.expanduser().absolute().parent
    return [root.resolve() for root in (video_dir, linc_home(), _system_tmp())]


def confine_out_dir(video: Path, out_dir: Path) -> Path:
    """Resolve *out_dir* (symlinks, ``..``) and refuse it outside the allowed roots."""
    resolved = out_dir.expanduser().resolve()
    if any(resolved.is_relative_to(root) for root in allowed_out_roots(video)):
        return resolved
    raise ValueError(
        f"out_dir {out_dir} resolves to {resolved}, outside the video's directory, "
        "$LINC_HOME and the system temp dir"
    )


def open_video(path: Path | str) -> Any:
    """Open an ``.mp4``/``.mov`` with the MP4 demuxer pinned — never sniffed."""
    path = Path(path)
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError(f"only .mp4/.mov recordings are accepted, got {path.name!r}")
    return av.open(str(path), format=DEMUXER)


def _video_stream(container: Any) -> Any:
    if not container.streams.video:
        raise ValueError("no video stream in file")
    stream = container.streams.video[0]
    check_frame_size(stream.codec_context.width, stream.codec_context.height)
    return stream


def check_frame_size(width: int, height: int) -> None:
    """Refuse a frame larger than :data:`MAX_FRAME_PIXELS` (a decompression bomb)."""
    if (width or 0) * (height or 0) > MAX_FRAME_PIXELS:
        raise ValueError(
            f"video frame {width}x{height} exceeds the {MAX_FRAME_PIXELS} pixels limit"
        )


def probe_duration(path: Path | str) -> float:
    """Duration of the video at *path* in seconds."""
    with open_video(path) as container:
        stream = _video_stream(container)
        return duration_from(container.duration, stream.duration, stream.time_base)


def _decode_frame_at(container: Any, stream: Any, t: float) -> tuple[Any, float]:
    """Seek to the keyframe before *t*, decode forward; return (frame, actual seconds)."""
    time_base = stream.time_base
    start = stream.start_time or 0
    target_pts = start + t / time_base
    container.seek(int(target_pts), stream=stream, backward=True, any_frame=False)
    frame = first_frame_at_or_after(container.decode(stream), target_pts)
    return frame, float((frame.pts - start) * time_base)


# O_NOFOLLOW: a frame_<ms>.png symlink planted in a shared out_dir (the system
# temp dir is an allowed root) must not redirect the write outside the roots.
_PNG_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW


def _save_png(image: Image.Image, dest: Path) -> None:
    """Write *image* as PNG to *dest*, never through a symlink."""
    with os.fdopen(os.open(dest, _PNG_FLAGS, 0o644), "wb") as handle:
        image.save(handle, format="PNG")


def _write_frame(frame: Any, t: float, t_actual: float, dest: Path) -> dict[str, Any]:
    check_frame_size(frame.width, frame.height)
    image = frame.to_image()
    mean, std = luma_stats(image)
    _save_png(image, dest)
    return {
        "t_s": t,
        "t_actual_s": round(t_actual, 4),
        "path": str(dest),
        "width": image.width,
        "height": image.height,
        "mean_luma": round(mean, 2),
        "black": is_black(mean, std),
    }


def extract_validated(path: Path, timestamps: Iterable[float], out_dir: Path) -> list[dict]:
    """Extract frames for already range-checked *timestamps* into *out_dir*."""
    wanted = unique_by_ms(timestamps)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    with open_video(path) as container:
        stream = _video_stream(container)
        for ms, t in wanted:
            frame, t_actual = _decode_frame_at(container, stream, t)
            results.append(_write_frame(frame, t, t_actual, out_dir / f"frame_{ms}.png"))
    log.info("frames_extracted", video=str(path), count=len(results))
    return results


def extract_frames(path: Path | str, timestamps: list[float], out_dir: Path | str) -> list[dict]:
    """Write ``<out_dir>/frame_<ms>.png`` for each timestamp; return per-frame dicts.

    Each dict: ``t_s`` (requested), ``t_actual_s`` (decoded pts — >= t_s,
    except inside the last frame's display interval, where the last frame
    is returned with t_actual_s < t_s), ``path``, ``width``, ``height``,
    ``mean_luma`` (0-255), ``black``. Raises ``ValueError`` — before writing
    anything — if *out_dir* resolves outside the allowed roots or any
    timestamp is outside ``0 <= t < duration``.
    """
    path = Path(path)
    target = confine_out_dir(path, Path(out_dir))
    duration = probe_duration(path)
    bad = out_of_range(timestamps, duration)
    if bad:
        raise OutOfRangeError(bad, duration)
    return extract_validated(path, timestamps, target)


def _validation_error(message: str, **extra: Any) -> dict[str, Any]:
    return {"error": message, "validation_error": True, **extra}


def _probe_checked(path: Path) -> float:
    """Duration of *path*, with missing/unreadable files as ``ValueError``."""
    if not path.is_file():
        raise ValueError(f"video not found: {path}")
    try:
        return probe_duration(path)
    except (ValueError, av.error.FFmpegError) as exc:
        raise ValueError(f"cannot read video {path}: {exc}") from None


def _validated_request(
    video_path: str, timestamps: str, out_dir: str
) -> tuple[Path, list[float], float, Path]:
    """Check every input before anything is written; raise ``ValueError`` if bad."""
    wanted = parse_timestamps(timestamps)
    path = Path(video_path).expanduser()
    target = confine_out_dir(path, Path(out_dir) if out_dir else default_out_dir(path))
    duration = _probe_checked(path)
    bad = out_of_range(wanted, duration)
    if bad:
        raise OutOfRangeError(bad, duration)
    return path, wanted, duration, target


def extract_request(video_path: str, timestamps: str, out_dir: str = "") -> dict[str, Any]:
    """Tool body: validate inputs, then extract. Returns a result or error dict."""
    try:
        path, wanted, duration, target = _validated_request(video_path, timestamps, out_dir)
    except ValueError as exc:
        return _validation_error(str(exc), **getattr(exc, "extra", {}))
    extracted = extract_validated(path, wanted, target)
    return {
        "duration_s": round(duration, 3),
        "frames": extracted,
        "black_frames": sum(1 for f in extracted if f["black"]),
    }
