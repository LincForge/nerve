"""Tiny MP4 fixtures built in-test with PyAV (codec "mpeg4", shipped by every FFmpeg).

``write_mp4`` produces a playable clip; ``write_empty_mp4`` reproduces what Wear OS
``adb shell screenrecord`` leaves behind: a non-zero-byte MP4 with one video stream
and no frames (reactor 25be2624, bead linc-nerve-ayv9).
"""

from __future__ import annotations

import functools
import tempfile
from fractions import Fraction
from pathlib import Path

import av
from PIL import Image

_SIZE = (64, 64)
_FPS = 10


def _add_video_stream(container: av.container.OutputContainer) -> av.VideoStream:
    stream = container.add_stream("mpeg4", rate=_FPS)
    stream.width, stream.height = _SIZE
    stream.pix_fmt = "yuv420p"
    return stream


def write_mp4(path: Path, frames: int = 3) -> Path:
    """Encode ``frames`` 64x64 frames to an MP4 at ``path``; return ``path``."""
    with av.open(str(path), mode="w", format="mp4") as container:
        stream = _add_video_stream(container)
        for i in range(frames):
            shade = (i * 80) % 256
            frame = av.VideoFrame.from_image(Image.new("RGB", _SIZE, (shade, shade, shade)))
            frame.pts = i
            frame.time_base = Fraction(1, _FPS)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def write_empty_mp4(path: Path) -> Path:
    """Write an MP4 with one video stream and no frames; return ``path``."""
    with av.open(str(path), mode="w", format="mp4") as container:
        _add_video_stream(container)
        container.start_encoding()  # write the header even though no frame follows
    return path


@functools.cache
def mp4_bytes(frames: int = 3) -> bytes:
    """Bytes of a ``write_mp4`` clip — a drop-in payload for a fake ``adb pull``."""
    with tempfile.TemporaryDirectory() as tmp:
        return write_mp4(Path(tmp) / "clip.mp4", frames=frames).read_bytes()
