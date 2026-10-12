"""Unit tests for the WDA MJPEG multipart parser (pure, device-independent).

Productionized from the T13 spike — the parser now lives in the linc_nerve package.
"""

from __future__ import annotations

from linc_nerve.core.wda_mjpeg import iter_mjpeg_frames

BOUNDARY = b"--BoundaryString"


def _part(payload: bytes, *, with_length: bool = True) -> bytes:
    headers = b"Content-type: image/jpeg\r\n"
    if with_length:
        headers += b"Content-Length: %d\r\n" % len(payload)
    return BOUNDARY + b"\r\n" + headers + b"\r\n" + payload + b"\r\n"


def test_single_frame_with_content_length():
    jpeg = b"\xff\xd8JPEG_ONE\xff\xd9"
    stream = [_part(jpeg)]
    assert list(iter_mjpeg_frames(stream, BOUNDARY)) == [jpeg]


def test_multiple_frames_one_chunk():
    a, b = b"\xff\xd8AAA\xff\xd9", b"\xff\xd8BBBB\xff\xd9"
    stream = [_part(a) + _part(b)]
    assert list(iter_mjpeg_frames(stream, BOUNDARY)) == [a, b]


def test_frame_split_across_chunks():
    jpeg = b"\xff\xd8SPLIT_PAYLOAD\xff\xd9"
    blob = _part(jpeg)
    cut = len(blob) - 5  # split mid-payload at an arbitrary offset
    stream = [blob[:cut], blob[cut:]]
    assert list(iter_mjpeg_frames(stream, BOUNDARY)) == [jpeg]


def test_boundary_delimited_no_content_length():
    a, b = b"\xff\xd8AAA\xff\xd9", b"\xff\xd8BB\xff\xd9"
    stream = [_part(a, with_length=False) + _part(b, with_length=False) + BOUNDARY + b"\r\n"]
    assert list(iter_mjpeg_frames(stream, BOUNDARY)) == [a, b]


def test_partial_trailing_part_is_not_yielded():
    jpeg = b"\xff\xd8DONE\xff\xd9"
    # A complete first part, then an incomplete next part header — must not yield the partial.
    stream = [_part(jpeg) + BOUNDARY + b"\r\nContent-type: image/jpeg\r\n"]
    assert list(iter_mjpeg_frames(stream, BOUNDARY)) == [jpeg]


def test_byte_at_a_time_chunking():
    """Pathological 1-byte chunks must still reassemble frames correctly."""
    a, b = b"\xff\xd8AAA\xff\xd9", b"\xff\xd8BBBB\xff\xd9"
    blob = _part(a) + _part(b)
    stream = [blob[i : i + 1] for i in range(len(blob))]
    assert list(iter_mjpeg_frames(stream, BOUNDARY)) == [a, b]
