"""Tests for scrcpy control-socket plumbing in ScrcpyStream.

Covers the connect/send plumbing (_connect_control, send_control, has_control)
that is kept for a possible future re-enable. The high-level tap/swipe/long_press
input layer and its message-packing module were removed in T12 (Android input
now routes through `adb shell input`; iOS through WDA).
"""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from linc_nerve.core.scrcpy import ScrcpyStream, _allocate_port


def _make_fake_scrcpy_server(
    port: int,
    *,
    accept_control: bool = True,
) -> tuple[threading.Thread, threading.Event, list[bytes]]:
    """Fake scrcpy-server that accepts video + control connections.

    Returns (thread, ready_event, received_control_messages).
    """
    ready = threading.Event()
    received: list[bytes] = []

    def serve() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(2)
        srv.settimeout(5)
        ready.set()

        try:
            # Accept video socket (first connection)
            video_conn, _ = srv.accept()
            video_conn.sendall(b"\x00")  # dummy byte
            # Codec meta: h264, 1080x1920
            video_conn.sendall(struct.pack(">III", 0x68323634, 1080, 1920))

            if accept_control:
                # Accept control socket (second connection)
                # No dummy byte in forward mode — control is raw TCP
                control_conn, _ = srv.accept()

                # Read control messages until connection closes
                control_conn.settimeout(2.0)
                try:
                    while True:
                        data = control_conn.recv(4096)
                        if not data:
                            break
                        received.append(data)
                except (TimeoutError, OSError):
                    pass
                finally:
                    control_conn.close()

            # Keep video open briefly
            time.sleep(0.3)
            video_conn.close()
        except (TimeoutError, OSError):
            pass
        finally:
            srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t, ready, received


class TestScrcpyStreamControlConnect:
    """ScrcpyStream should connect a control socket after video."""

    def test_has_control_sock_attribute(self) -> None:
        stream = ScrcpyStream("TEST", port=12345)
        assert hasattr(stream, "_control_sock")
        assert stream._control_sock is None

    def test_has_control_property(self) -> None:
        stream = ScrcpyStream("TEST", port=12345)
        assert stream.has_control is False

    def test_connect_control_socket(self) -> None:
        """After video connect, connect_control should establish second socket."""
        port = _allocate_port()
        thread, ready, _ = _make_fake_scrcpy_server(port)
        ready.wait(timeout=2)

        stream = ScrcpyStream("TEST", port=port)
        # Connect video socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(("127.0.0.1", port))
        sock.recv(1)  # dummy byte
        stream._sock = sock

        # Connect control socket
        stream._connect_control()
        assert stream._control_sock is not None

        # Cleanup
        stream._control_sock.close()
        sock.close()
        thread.join(timeout=3)

    def test_send_control_delivers_bytes(self) -> None:
        """send_control should deliver raw bytes to the control socket."""
        port = _allocate_port()
        thread, ready, received = _make_fake_scrcpy_server(port)
        ready.wait(timeout=2)

        stream = ScrcpyStream("TEST", port=port)
        # Connect video
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(("127.0.0.1", port))
        sock.recv(1)
        stream._sock = sock

        # Connect control
        stream._connect_control()

        # Send an opaque 32-byte control payload — send_control is raw-byte
        # transport; the message format lived in the removed packing layer.
        msg = bytes([2]) + bytes(range(31))
        stream.send_control(msg)

        # Close to flush
        time.sleep(0.1)
        stream._control_sock.close()
        stream._control_sock = None
        sock.close()
        thread.join(timeout=3)

        # Verify the server received the exact bytes.
        assert b"".join(received) == msg

    def test_send_control_raises_without_socket(self) -> None:
        """send_control should raise if control socket not connected."""
        stream = ScrcpyStream("TEST", port=12345)
        with pytest.raises(RuntimeError, match=r"[Cc]ontrol.*not connected"):
            stream.send_control(b"\x00" * 32)
