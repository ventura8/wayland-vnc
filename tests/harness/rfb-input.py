"""Minimal RFB client that injects keyboard and pointer events into the harness WayVNC.

Only the unauthenticated 'None' security type over the harness unix socket is
supported; this never talks to a fixture under test.
"""

import argparse
import socket
import struct
import sys
import time

KEYSYMS = {"\n": 0xFF0D, "\t": 0xFF09, " ": 0x0020}


def keysym(char: str) -> int:
    """X keysym for a character: Latin-1 is its own code point; anything beyond is
    the Unicode keysym range (code point | 0x01000000), as xkbcommon and servers map it."""
    if char in KEYSYMS:
        return KEYSYMS[char]
    point = ord(char)
    return point if point <= 0xFF else 0x01000000 | point


class Client:
    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(path)
        # self.recv loops until all 12 bytes arrive; a bare recv can return short.
        version = self.recv(12)
        if not version.startswith(b"RFB "):
            raise SystemExit(f"unexpected server greeting {version!r}")
        self.sock.sendall(b"RFB 003.008\n")
        count = self.recv(1)[0]
        types = self.recv(count)
        if 1 not in types:
            raise SystemExit(f"harness server does not offer security None: {list(types)}")
        self.sock.sendall(bytes([1]))
        if struct.unpack(">I", self.recv(4))[0] != 0:
            raise SystemExit("security handshake failed")
        self.sock.sendall(bytes([1]))  # shared
        init = self.recv(24)
        self.width, self.height = struct.unpack(">HH", init[:4])
        name_length = struct.unpack(">I", init[20:24])[0]
        self.recv(name_length)
        self.buttons = 0

    def recv(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise SystemExit("harness server closed the connection")
            data += chunk
        return data

    def key(self, sym: int, down: bool) -> None:
        self.sock.sendall(struct.pack(">BBxxI", 4, int(down), sym))

    def type_text(self, text: str) -> None:
        for char in text:
            sym = keysym(char)
            self.key(sym, True)
            time.sleep(0.02)
            self.key(sym, False)
            time.sleep(0.02)

    def type_secret(self, secret: str) -> None:
        """Type a secret into a lock and confirm with Return.

        A freshly focused virtual keyboard drops its first key event, so a warm-up
        key and a Backspace precede the secret; the lock's buffer then holds exactly
        the secret. Backspace on an empty buffer is a no-op in every locker tested."""
        self.key(0x0061, True)
        time.sleep(0.05)
        self.key(0x0061, False)
        time.sleep(0.05)
        self.key(0xFF08, True)  # BackSpace erases the warm-up key
        time.sleep(0.05)
        self.key(0xFF08, False)
        time.sleep(0.1)
        self.type_text(secret)
        time.sleep(0.2)
        self.key(0xFF0D, True)  # Return
        time.sleep(0.05)
        self.key(0xFF0D, False)

    def pointer(self, x_pos: int, y_pos: int, buttons: int | None = None) -> None:
        if buttons is not None:
            self.buttons = buttons
        self.sock.sendall(struct.pack(">BBHH", 5, self.buttons, x_pos, y_pos))
        time.sleep(0.03)

    def click(self, x_pos: int, y_pos: int) -> None:
        self.pointer(x_pos, y_pos, 0)
        self.pointer(x_pos, y_pos, 1)
        self.pointer(x_pos, y_pos, 0)

    def scroll(self, x_pos: int, y_pos: int, ticks: int = 3) -> None:
        self.pointer(x_pos, y_pos, 0)
        for _ in range(ticks):
            self.pointer(x_pos, y_pos, 1 << 4)  # wheel down
            self.pointer(x_pos, y_pos, 0)

    def capture(self, path: str) -> None:
        """Fetch one raw full-frame update and write it as a binary PPM (no PIL needed)."""
        # SetPixelFormat: 32 bpp, depth 24, little endian, true colour, 8-bit RGB at 16/8/0.
        pixel_format = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        self.sock.sendall(struct.pack(">Bxxx", 0) + pixel_format)
        self.sock.sendall(struct.pack(">BxH", 2, 1) + struct.pack(">i", 0))
        self.sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height))
        frame = bytearray(self.width * self.height * 3)
        while True:
            kind = self.recv(1)[0]
            if kind != 0:
                raise SystemExit(f"unexpected server message {kind}")
            count = struct.unpack(">xH", self.recv(3))[0]
            for _ in range(count):
                x_pos, y_pos, width, height, encoding = struct.unpack(">HHHHi", self.recv(12))
                if encoding != 0:
                    raise SystemExit(f"unexpected encoding {encoding}")
                data = self.recv(width * height * 4)
                for row in range(height):
                    src = data[row * width * 4 : (row + 1) * width * 4]
                    dst = ((y_pos + row) * self.width + x_pos) * 3
                    # BGRX to RGB a row at a time: the per-pixel loop was the slowest
                    # part of a full-frame capture.
                    line = bytearray(width * 3)
                    line[0::3] = src[2::4]
                    line[1::3] = src[1::4]
                    line[2::3] = src[0::4]
                    frame[dst : dst + width * 3] = line
            if count:
                break
        with open(path, "wb") as sink:
            sink.write(f"P6\n{self.width} {self.height}\n255\n".encode())
            sink.write(frame)

    def drag(self, x_from: int, y_from: int, x_to: int, y_to: int, steps: int = 20) -> None:
        self.pointer(x_from, y_from, 0)
        self.pointer(x_from, y_from, 1)
        for step in range(1, steps + 1):
            x_pos = x_from + (x_to - x_from) * step // steps
            y_pos = y_from + (y_to - y_from) * step // steps
            self.pointer(x_pos, y_pos, 1)
        self.pointer(x_to, y_to, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("size")
    typer = sub.add_parser("type")
    typer.add_argument("text")
    click = sub.add_parser("click")
    click.add_argument("x", type=int)
    click.add_argument("y", type=int)
    scroll = sub.add_parser("scroll")
    scroll.add_argument("x", type=int)
    scroll.add_argument("y", type=int)
    drag = sub.add_parser("drag")
    for name in ("x1", "y1", "x2", "y2"):
        drag.add_argument(name, type=int)
    capture = sub.add_parser("capture")
    capture.add_argument("path")
    key = sub.add_parser("key")
    key.add_argument("keysym", help="X keysym, e.g. 0xff0d for Return")
    sub.add_parser("secret")
    args = parser.parse_args()
    client = Client(args.socket)
    if args.command == "size":
        print(f"{client.width}x{client.height}")
    elif args.command == "type":
        # "-" reads the text from stdin so secrets never appear on a command line.
        client.type_text(sys.stdin.read().rstrip("\n") if args.text == "-" else args.text)
    elif args.command == "click":
        client.click(args.x, args.y)
    elif args.command == "scroll":
        client.scroll(args.x, args.y)
    elif args.command == "drag":
        client.drag(args.x1, args.y1, args.x2, args.y2)
    elif args.command == "capture":
        client.capture(args.path)
    elif args.command == "key":
        sym = int(args.keysym, 0)
        client.key(sym, True)
        time.sleep(0.05)
        client.key(sym, False)
    elif args.command == "secret":
        client.type_secret(sys.stdin.readline().rstrip("\n"))
    time.sleep(0.3)
    client.sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
