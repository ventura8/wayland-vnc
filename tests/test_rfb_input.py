"""Wire-format tests for the harness RFB input client, over an in-process socket pair.

These never touch a real compositor or viewer; they pin the bytes the client sends so
the harness keeps injecting valid keyboard, pointer and framebuffer-request messages.
"""

import importlib.util
import socket
import struct
import threading
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "rfb_input", Path(__file__).resolve().parents[1] / "tests" / "harness" / "rfb-input.py"
)
rfb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rfb)


class FakeServer:
    """A minimal RFB 3.8 server that completes the handshake and records client messages."""

    def __init__(self, sock, width=1920, height=1080):
        self.sock = sock
        self.width = width
        self.height = height
        self.messages = bytearray()

    def handshake(self):
        self.sock.sendall(b"RFB 003.008\n")
        self._recv(12)  # client version
        self.sock.sendall(bytes([1, 1]))  # one security type: None
        assert self._recv(1) == bytes([1])
        self.sock.sendall(struct.pack(">I", 0))  # SecurityResult OK
        self._recv(1)  # ClientInit (shared flag)
        name = b"fixture"
        self.sock.sendall(
            struct.pack(">HH", self.width, self.height)
            + bytes(16)
            + struct.pack(">I", len(name))
            + name
        )

    def _recv(self, size):
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def serve_framebuffer(self):
        """Answer one FramebufferUpdateRequest with a single raw blue rectangle.

        The client sends exactly a 20-byte SetPixelFormat, an 8-byte SetEncodings and
        a 10-byte FramebufferUpdateRequest before it blocks on the reply."""
        self.collect(until=38)
        pixels = bytes((255, 0, 0, 0)) * (self.width * self.height)  # blue in BGRx
        header = struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, self.width, self.height, 0)
        self.sock.sendall(header + pixels)

    def collect(self, until):
        while len(self.messages) < until:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            self.messages += chunk


@pytest.fixture(name="pair")
def socket_pair():
    server_sock, client_sock = socket.socketpair()
    yield server_sock, client_sock
    server_sock.close()
    client_sock.close()


def run_server(server, method):
    """Run the fake server on a thread, keeping any exception it raises.

    A bare thread swallows failures: the handshake could raise and the test would go
    on to assert against a server that never ran, usually passing for the wrong
    reason or hanging until the join timeout and continuing anyway.
    """
    failures = []

    def body():
        try:
            server.handshake()
            method(server)
        except Exception as error:  # surfaced by join_server in the test's own thread
            failures.append(error)

    thread = threading.Thread(target=body)
    thread.failures = failures
    thread.start()
    return thread


def join_server(thread, timeout=5):
    thread.join(timeout)
    assert not thread.is_alive(), "the fake RFB server thread did not finish"
    if thread.failures:
        raise thread.failures[0]


def test_handshake_reads_geometry(pair):
    server_sock, client_sock = pair
    server = FakeServer(server_sock)
    thread = run_server(server, lambda _s: None)
    client = rfb.Client.__new__(rfb.Client)
    client.sock = client_sock
    client.buttons = 0
    # Re-run the handshake body against the fake server.
    version = client.sock.recv(12)
    assert version.startswith(b"RFB ")
    client.sock.sendall(b"RFB 003.008\n")
    count = client.sock.recv(1)[0]
    client.sock.recv(count)
    client.sock.sendall(bytes([1]))
    assert struct.unpack(">I", client.recv(4))[0] == 0
    client.sock.sendall(bytes([1]))
    init = client.recv(24)
    width, height = struct.unpack(">HH", init[:4])
    client.recv(struct.unpack(">I", init[20:24])[0])
    assert (width, height) == (1920, 1080)
    join_server(thread)


def test_keysyms_cover_latin1_directly_and_the_rest_through_the_unicode_range():
    assert rfb.keysym("a") == 0x61
    assert rfb.keysym("\n") == 0xFF0D
    assert rfb.keysym("é") == 0xE9
    assert rfb.keysym("ș") == 0x01000219
    assert rfb.keysym("€") == 0x010020AC


def test_key_and_pointer_message_bytes(pair):
    server_sock, client_sock = pair
    server = FakeServer(server_sock)
    thread = run_server(server, lambda s: s.collect(until=8 + 6))
    client = _connected_client(client_sock)
    client.key(0xFF0D, True)
    client.pointer(100, 200, 1)
    join_server(thread)
    key_msg = struct.pack(">BBxxI", 4, 1, 0xFF0D)
    pointer_msg = struct.pack(">BBHH", 5, 1, 100, 200)
    assert key_msg in bytes(server.messages)
    assert pointer_msg in bytes(server.messages)


def test_click_scroll_and_drag_send_button_transitions(pair):
    server_sock, client_sock = pair
    server = FakeServer(server_sock)
    thread = run_server(server, lambda s: s.collect(until=90))  # 15 pointer messages
    client = _connected_client(client_sock)
    client.click(10, 10)
    client.scroll(20, 20, ticks=2)
    client.drag(0, 0, 200, 0, steps=4)
    join_server(thread)
    stream = bytes(server.messages)
    # A wheel-down button mask (1<<4) must appear for the scroll.
    assert struct.pack(">BBHH", 5, 1 << 4, 20, 20) in stream
    # The drag holds the left button down through an intermediate point.
    assert struct.pack(">BBHH", 5, 1, 50, 0) in stream


def test_capture_writes_a_ppm(pair, tmp_path):
    server_sock, client_sock = pair
    server = FakeServer(server_sock, width=8, height=4)
    thread = run_server(server, FakeServer.serve_framebuffer)
    client = _connected_client(client_sock, width=8, height=4)
    out = tmp_path / "frame.ppm"
    client.capture(str(out))
    join_server(thread)
    data = out.read_bytes()
    assert data.startswith(b"P6\n8 4\n255\n")
    body = data.split(b"255\n", 1)[1]
    assert body[:3] == bytes((0, 0, 255))  # BGRx blue decodes to RGB blue in the PPM


def _connected_client(client_sock, width=1920, height=1080):
    client = rfb.Client.__new__(rfb.Client)
    client.sock = client_sock
    client.buttons = 0
    client.sock.recv(12)
    client.sock.sendall(b"RFB 003.008\n")
    count = client.sock.recv(1)[0]
    client.sock.recv(count)
    client.sock.sendall(bytes([1]))
    client.recv(4)
    client.sock.sendall(bytes([1]))
    init = client.recv(24)
    client.width, client.height = struct.unpack(">HH", init[:4])
    name_length = struct.unpack(">I", init[20:24])[0]
    client.recv(name_length)  # consume the desktop name, as the real client does
    assert (client.width, client.height) == (width, height)
    return client
