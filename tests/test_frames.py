"""nerve_extract_frames — PNG frames at given timestamps from a host MP4.

Bead linc-nerve-5l2 (epic linc-nerve-c9i, bug-hunt §6/§7.9). The fixture is a
tiny MP4 generated in ``tmp_path`` with PyAV — 3 s at 10 fps, first half
black, second half white — so no device, adb or ffmpeg binary is involved.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import threading
import time
from fractions import Fraction

import av
import pytest
from PIL import Image

from linc_nerve.core import frames
from linc_nerve.core.frames import (
    _video_stream,
    duration_from,
    extract_frames,
    first_frame_at_or_after,
    is_black,
    out_of_range,
    parse_timestamps,
    probe_duration,
    unique_by_ms,
)
from linc_nerve.mcp.recording_tools import _nerve_extract_frames_sync

_FPS = 10
_SECONDS = 3
_SIZE = (64, 48)


def _write_fixture(path: pathlib.Path) -> pathlib.Path:
    """3 s / 10 fps MP4: frames 0-14 black, frames 15-29 white."""
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("mpeg4", rate=_FPS)
        stream.width, stream.height = _SIZE
        stream.pix_fmt = "yuv420p"
        stream.codec_context.gop_size = 5
        total = _FPS * _SECONDS
        for i in range(total):
            colour = (0, 0, 0) if i < total // 2 else (255, 255, 255)
            frame = av.VideoFrame.from_image(Image.new("RGB", _SIZE, colour))
            frame.pts = i
            frame.time_base = Fraction(1, _FPS)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


@pytest.fixture
def fixture_mp4(tmp_path: pathlib.Path) -> pathlib.Path:
    return _write_fixture(tmp_path / "fixture.mp4")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_is_black_dark_and_flat() -> None:
    assert is_black(3.0, 1.0) is True


def test_is_black_dark_but_textured_is_not_black() -> None:
    assert is_black(3.0, 20.0) is False


def test_is_black_bright_is_not_black() -> None:
    assert is_black(120.0, 1.0) is False


def test_duration_from_prefers_container_duration() -> None:
    # container duration is in av.time_base (microseconds)
    assert duration_from(3_000_000, 999, Fraction(1, 10)) == pytest.approx(3.0)


def test_duration_from_falls_back_to_stream_duration() -> None:
    assert duration_from(None, 30, Fraction(1, 10)) == pytest.approx(3.0)


def test_duration_from_raises_when_unknown() -> None:
    with pytest.raises(ValueError, match="duration"):
        duration_from(None, None, Fraction(1, 10))


def test_parse_timestamps_comma_list() -> None:
    assert parse_timestamps("1.0, 4.2,0") == [1.0, 4.2, 0.0]


def test_parse_timestamps_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="abc"):
        parse_timestamps("1.0,abc")


def test_parse_timestamps_rejects_empty() -> None:
    with pytest.raises(ValueError):
        parse_timestamps(" , ")


def test_out_of_range_lists_negative_and_past_end() -> None:
    assert out_of_range([-0.1, 0.0, 1.5, 4.2], 3.0) == [-0.1, 4.2]


def test_out_of_range_rejects_t_equal_to_duration() -> None:
    # The range is half-open, [0, duration): no frame *starts* at t == duration.
    assert out_of_range([0.0, 2.999, 3.0], 3.0) == [3.0]


# ---------------------------------------------------------------------------
# Core: probe + extract against the real PyAV fixture
# ---------------------------------------------------------------------------


def test_probe_duration_within_tolerance(fixture_mp4: pathlib.Path) -> None:
    assert abs(probe_duration(fixture_mp4) - 3.0) <= 0.15


def test_extract_frames_black_then_white(fixture_mp4: pathlib.Path, tmp_path) -> None:
    out_dir = tmp_path / "out"
    result = extract_frames(fixture_mp4, [0.5, 2.5], out_dir)

    assert len(result) == 2
    pngs = sorted(out_dir.glob("*.png"))
    assert len(pngs) == 2

    early, late = result
    assert early["t_s"] == 0.5
    assert late["t_s"] == 2.5
    assert early["black"] is True
    assert late["black"] is False
    for entry in result:
        assert entry["t_actual_s"] >= entry["t_s"] - 1e-6
        assert entry["t_actual_s"] - entry["t_s"] <= 0.15
        assert pathlib.Path(entry["path"]).is_file()
        assert (entry["width"], entry["height"]) == _SIZE
        assert 0.0 <= entry["mean_luma"] <= 255.0
    assert pathlib.Path(early["path"]).name == "frame_500.png"
    assert late["mean_luma"] > 200


def test_extract_frames_duplicates_produce_one_file(fixture_mp4, tmp_path) -> None:
    out_dir = tmp_path / "dups"
    result = extract_frames(fixture_mp4, [1.0, 1.0, 1.0004], out_dir)
    assert len(result) == 1
    assert len(list(out_dir.glob("*.png"))) == 1


def test_extract_frames_rejects_t_equal_to_duration(fixture_mp4, tmp_path) -> None:
    out_dir = tmp_path / "end"
    with pytest.raises(ValueError, match=r"timestamps outside the video.*: 3$"):
        extract_frames(fixture_mp4, [3.0], out_dir)
    assert not out_dir.exists()


def test_extract_frames_inside_last_frame_interval_returns_last_frame(
    fixture_mp4, tmp_path
) -> None:
    # 2.95 s lies inside the last frame's display interval (pts 2.9 s .. 3.0 s):
    # no frame starts at or after it, so the frame on screen (the last) is
    # returned and t_actual_s < t_s — the one documented exception.
    (entry,) = extract_frames(fixture_mp4, [2.95], tmp_path / "tail")
    assert entry["black"] is False
    assert entry["t_actual_s"] < entry["t_s"]
    assert entry["t_s"] - entry["t_actual_s"] <= 0.15


def test_extract_frames_refuses_out_of_range(fixture_mp4, tmp_path) -> None:
    out_dir = tmp_path / "never"
    with pytest.raises(ValueError, match=r"4\.2"):
        extract_frames(fixture_mp4, [1.0, 4.2], out_dir)
    assert not out_dir.exists()


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


def test_tool_rejects_out_of_range_and_writes_nothing(fixture_mp4, tmp_path) -> None:
    out_dir = tmp_path / "tool_out"
    result = _nerve_extract_frames_sync(str(fixture_mp4), "1.0,4.2", str(out_dir))

    assert "error" in result
    assert result["validation_error"] is True
    assert "4.2" in result["error"]
    assert result["out_of_range"] == [4.2]
    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_tool_rejects_t_equal_to_duration_and_writes_nothing(fixture_mp4, tmp_path) -> None:
    out_dir = tmp_path / "tool_end"
    result = _nerve_extract_frames_sync(str(fixture_mp4), "1.0,3", str(out_dir))

    assert result["validation_error"] is True
    assert result["out_of_range"] == [3.0]
    assert not out_dir.exists()


def test_tool_success_shape(fixture_mp4, tmp_path) -> None:
    out_dir = tmp_path / "tool_ok"
    result = _nerve_extract_frames_sync(str(fixture_mp4), "0.5,2.5", str(out_dir))

    assert "error" not in result
    assert abs(result["duration_s"] - 3.0) <= 0.15
    assert len(result["frames"]) == 2
    assert result["black_frames"] == 1


def test_tool_default_out_dir_sits_next_to_video(fixture_mp4) -> None:
    result = _nerve_extract_frames_sync(str(fixture_mp4), "2.0")
    frame_path = pathlib.Path(result["frames"][0]["path"])
    assert frame_path.parent == fixture_mp4.parent / "fixture_frames"


def test_tool_missing_video_is_validation_error(tmp_path) -> None:
    result = _nerve_extract_frames_sync(str(tmp_path / "nope.mp4"), "1.0", str(tmp_path))
    assert result["validation_error"] is True
    assert "nope.mp4" in result["error"]


def test_tool_bad_timestamps_is_validation_error(fixture_mp4, tmp_path) -> None:
    result = _nerve_extract_frames_sync(str(fixture_mp4), "1.0,soon", str(tmp_path))
    assert result["validation_error"] is True
    assert "soon" in result["error"]


def test_tool_unreadable_video_is_error(tmp_path) -> None:
    bogus = tmp_path / "bogus.mp4"
    bogus.write_bytes(b"not a video")
    result = _nerve_extract_frames_sync(str(bogus), "1.0", str(tmp_path / "o"))
    assert "error" in result
    assert result.get("validation_error") is True


def test_tool_is_registered_as_global() -> None:
    from linc_nerve.mcp import _core

    meta = _core.TOOL_REGISTRY["nerve_extract_frames"]
    assert meta["kind"] == "global"


# ---------------------------------------------------------------------------
# Seams: frame selection + stream lookup with fakes
# ---------------------------------------------------------------------------


class _FakeFrame:
    def __init__(self, pts: int | None) -> None:
        self.pts = pts


def test_first_frame_at_or_after_skips_pts_less_frames() -> None:
    picked = first_frame_at_or_after([_FakeFrame(None), _FakeFrame(3), _FakeFrame(5)], 4)
    assert picked.pts == 5


def test_first_frame_at_or_after_falls_back_to_last_frame() -> None:
    picked = first_frame_at_or_after([_FakeFrame(1), _FakeFrame(2)], 9)
    assert picked.pts == 2


def test_first_frame_at_or_after_raises_when_nothing_decodes() -> None:
    with pytest.raises(ValueError, match="no decodable"):
        first_frame_at_or_after([_FakeFrame(None)], 0)


def test_unique_by_ms_sorts_and_dedupes() -> None:
    assert unique_by_ms([2.5, 0.5, 2.5004, 0.5]) == [(500, 0.5), (2500, 2.5)]


def test_video_stream_missing_raises() -> None:
    class _Streams:
        video: tuple = ()

    class _Container:
        streams = _Streams()

    with pytest.raises(ValueError, match="no video stream"):
        _video_stream(_Container())


# ---------------------------------------------------------------------------
# Hardening: pinned demuxer + suffix allow-list (no container sniffing)
# ---------------------------------------------------------------------------


def test_every_open_pins_the_mp4_demuxer(fixture_mp4, tmp_path, monkeypatch) -> None:
    real_open = av.open
    calls: list[dict] = []

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(frames.av, "open", spy)
    extract_frames(fixture_mp4, [0.5], tmp_path / "pinned")

    assert calls, "expected PyAV to be opened"
    assert all(call.get("format") == "mp4" for call in calls)


@pytest.mark.parametrize("name", ["clip.mkv", "clip.m3u8", "clip.mp4.txt", "clip"])
def test_tool_rejects_non_mp4_suffix(fixture_mp4, tmp_path, name) -> None:
    renamed = tmp_path / name
    shutil.copyfile(fixture_mp4, renamed)
    out_dir = tmp_path / "suffix_out"

    result = _nerve_extract_frames_sync(str(renamed), "0.5", str(out_dir))

    assert result["validation_error"] is True
    assert ".mp4" in result["error"]
    assert not out_dir.exists()


def test_tool_accepts_mov_suffix_any_case(fixture_mp4, tmp_path) -> None:
    renamed = tmp_path / "CLIP.MOV"
    shutil.copyfile(fixture_mp4, renamed)

    result = _nerve_extract_frames_sync(str(renamed), "2.5", str(tmp_path / "mov_out"))

    assert "error" not in result
    assert result["frames"][0]["black"] is False


def _run_watching_fifo(fifo: pathlib.Path, call) -> tuple[dict, bool]:
    """Run *call* on a thread; report whether anything opened *fifo* to read it.

    A FIFO is the referenced "file": opening its write end non-blocking only
    succeeds while a reader holds it, so a demuxer that follows the playlist
    is caught in the act (and released with EOF) instead of reading data.
    """
    out: dict = {}
    worker = threading.Thread(target=lambda: out.update(result=call()), daemon=True)
    worker.start()
    touched = False
    deadline = time.monotonic() + 20.0
    while worker.is_alive() and time.monotonic() < deadline:
        try:
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:  # ENXIO: nobody has the referenced file open
            time.sleep(0.005)
            continue
        touched = True
        os.close(fd)
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "extract_frames hung on a disguised playlist"
    return out["result"], touched


@pytest.mark.parametrize(
    "playlist",
    [
        "#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3.0,\nreferenced.mp4\n#EXT-X-ENDLIST\n",
        "ffconcat version 1.0\nfile referenced.mp4\n",
    ],
    ids=["hls", "ffconcat"],
)
def test_tool_rejects_playlist_named_mp4_without_touching_referenced_files(
    tmp_path, playlist
) -> None:
    referenced = tmp_path / "referenced.mp4"
    os.mkfifo(referenced)
    disguised = tmp_path / "x.mp4"
    disguised.write_text(playlist)
    out_dir = tmp_path / "playlist_out"

    result, touched = _run_watching_fifo(
        referenced,
        lambda: _nerve_extract_frames_sync(str(disguised), "0.5", str(out_dir)),
    )

    assert touched is False, "the playlist's referenced file was opened"
    assert result["validation_error"] is True
    assert not out_dir.exists()


def test_fifo_watcher_sees_an_hls_demuxer_follow_the_playlist(tmp_path) -> None:
    # Canary for the test above: FFmpeg's own HLS probe may decline a playlist
    # named .mp4, so prove the watcher would catch the HLS demuxer opening the
    # referenced segment if anything ever let it run on the disguised file.
    referenced = tmp_path / "referenced.mp4"
    os.mkfifo(referenced)
    disguised = tmp_path / "x.mp4"
    disguised.write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3.0,\nreferenced.mp4\n#EXT-X-ENDLIST\n"
    )

    def forced_hls() -> str:
        try:
            av.open(str(disguised), format="hls").close()
        except av.error.FFmpegError as exc:
            return type(exc).__name__
        return "opened"

    _, touched = _run_watching_fifo(referenced, forced_hls)

    assert touched is True


# ---------------------------------------------------------------------------
# Hardening: out_dir confinement
# ---------------------------------------------------------------------------


@pytest.fixture
def confined(tmp_path, monkeypatch) -> dict[str, pathlib.Path]:
    """A video in its own dir, plus a fake $LINC_HOME and fake system tmp.

    ``tmp_path`` itself sits under the real system tmp dir, so the tmp root
    is re-pointed at a sibling directory to make "elsewhere" really outside.
    """
    roots = {name: tmp_path / name for name in ("videos", "linc_home", "sys_tmp", "elsewhere")}
    for root in roots.values():
        root.mkdir()
    monkeypatch.setenv("LINC_HOME", str(roots["linc_home"]))
    monkeypatch.setattr(frames, "_system_tmp", lambda: roots["sys_tmp"])
    roots["video"] = _write_fixture(roots["videos"] / "clip.mp4")
    return roots


def _escape_via_symlink(roots: dict[str, pathlib.Path]) -> pathlib.Path:
    link = roots["videos"] / "link"
    link.symlink_to(roots["elsewhere"], target_is_directory=True)
    return link / "frames"


@pytest.mark.parametrize(
    "make_out_dir",
    [
        lambda r: r["elsewhere"] / "frames",
        lambda r: r["videos"] / ".." / "elsewhere" / "frames",
        _escape_via_symlink,
    ],
    ids=["outside", "dotdot", "symlink"],
)
def test_tool_refuses_out_dir_outside_allowed_roots(confined, make_out_dir) -> None:
    out_dir = make_out_dir(confined)

    result = _nerve_extract_frames_sync(str(confined["video"]), "0.5", str(out_dir))

    assert result["validation_error"] is True
    assert "out_dir" in result["error"]
    assert not any(confined["elsewhere"].iterdir())


@pytest.mark.parametrize("root", ["videos", "linc_home", "sys_tmp"])
def test_tool_allows_out_dir_under_each_allowed_root(confined, root) -> None:
    out_dir = confined[root] / "nested" / "frames"

    result = _nerve_extract_frames_sync(str(confined["video"]), "0.5", str(out_dir))

    assert "error" not in result
    assert pathlib.Path(result["frames"][0]["path"]).parent == out_dir


def test_extract_frames_library_refuses_out_dir_outside_roots(confined) -> None:
    with pytest.raises(ValueError, match="out_dir"):
        extract_frames(confined["video"], [0.5], confined["elsewhere"] / "frames")
    assert not any(confined["elsewhere"].iterdir())
