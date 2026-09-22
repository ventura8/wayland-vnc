#!/usr/bin/env bash
# Reproduce two LibVNCServer defects against PRISTINE upstream 0.9.15 and its own
# examples/server/example.c, so an upstream maintainer can run one command and see
# them. Nothing from this project is involved except the probe client, and with
# `--with-fix` the two candidate fixes are applied to show the difference.
#
#   A  ServerInit announces depth 32 for an 8/3/4 screen, but the ZRLE encoder sends
#      3-byte CPIXELs. A client that keeps the server's format decodes garbage.
#      Candidate fix: rfbGetScreen sets depth from bitsPerSample*samplesPerPixel.
#   B  rfbCheckFds processes ONE client message per pass and an update is flushed
#      between passes, so an update can be encoded in the old pixel format although
#      the client's SetPixelFormat is already in the socket buffer.
#      Candidate fix: drain the socket before returning to the send path.
#
# Everything happens inside `docker run --rm`; no host port is opened.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH

with_fix=no
case "${1:-}" in
"") ;;
--with-fix) with_fix=yes ;;
*)
  echo "usage: $0 [--with-fix]" >&2
  exit 2
  ;;
esac

tarball_url=https://codeload.github.com/LibVNC/libvncserver/tar.gz/refs/tags/LibVNCServer-0.9.15
tarball_sha=62352c7795e231dfce044beb96156065a05a05c974e5de9e023d688d8ff675d7

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cat >"$work/probe.py" <<'PROBE'
"""Two RFB 3.8 probes against examples/server/example (no authentication)."""
import socket
import struct
import sys
import time
import zlib

PORT = 5999
PF8 = struct.pack(">BBBBHHHBBBxxx", 8, 6, 0, 1, 3, 3, 3, 4, 2, 0)
PF32 = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)


def rd(sock, count):
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise EOFError(f"server closed after {len(buf)}/{count} bytes")
        buf += chunk
    return buf


def handshake(encodings):
    sock = socket.create_connection(("127.0.0.1", PORT), timeout=20)
    rd(sock, 12)
    sock.sendall(b"RFB 003.008\n")
    types = rd(sock, rd(sock, 1)[0])
    if 1 not in types:
        raise SystemExit(f"example server offered security types {list(types)}, expected None(1)")
    sock.sendall(b"\x01")
    if struct.unpack(">I", rd(sock, 4))[0] != 0:
        raise SystemExit("security handshake failed")
    sock.sendall(b"\x01")
    width, height = struct.unpack(">HH", rd(sock, 4))
    pixel_format = rd(sock, 16)
    rd(sock, struct.unpack(">I", rd(sock, 4))[0])
    sock.sendall(struct.pack(">BBH", 2, 0, len(encodings)) + b"".join(
        struct.pack(">i", enc) for enc in encodings))
    return sock, width, height, pixel_format


def tiles_parse_with(data, width, height, cpixel):
    """Whether the inflated ZRLE stream is self-consistent at this pixel size."""
    pos = 0
    try:
        for top in range(0, height, 64):
            for left in range(0, width, 64):
                wide, high = min(64, width - left), min(64, height - top)
                sub = data[pos]
                pos += 1
                if sub == 0:
                    pos += wide * high * cpixel
                elif sub == 1:
                    pos += cpixel
                elif 2 <= sub <= 16:
                    pos += sub * cpixel
                    bits = 1 if sub == 2 else 2 if sub <= 4 else 4
                    pos += ((wide * bits + 7) // 8) * high
                elif sub == 128 or sub >= 130:
                    palette = 0 if sub == 128 else sub - 128
                    pos += palette * cpixel
                    count = 0
                    while count < wide * high:
                        if sub == 128:
                            pos += cpixel
                            run = 1
                        else:
                            index = data[pos]
                            pos += 1
                            run = 1
                            if not index & 0x80:
                                count += 1
                                continue
                        while True:
                            more = data[pos]
                            pos += 1
                            run += more
                            if more != 255:
                                break
                        count += run
                    if count != wide * high:
                        return False
                else:
                    return False
        return pos == len(data)
    except IndexError:
        return False


def read_update(sock, inflater, width, height, bpp):
    """Return the list of (encoding, cpixel sizes the tiles parse with)."""
    kind = rd(sock, 1)[0]
    if kind != 0:
        raise SystemExit(f"unexpected server message type {kind}")
    rects = struct.unpack(">xH", rd(sock, 3))[0]
    found = []
    for _ in range(rects):
        _x, _y, wide, high, encoding = struct.unpack(">HHHHi", rd(sock, 12))
        if encoding == 16:
            payload = rd(sock, struct.unpack(">I", rd(sock, 4))[0])
            data = inflater.decompress(payload)
            found.append((encoding, [c for c in (1, 2, 3, 4)
                                     if tiles_parse_with(data, wide, high, c)]))
        elif encoding == 0:
            rd(sock, wide * high * bpp // 8)
            found.append((encoding, []))
        else:
            raise SystemExit(f"unexpected encoding {encoding}")
    return found


def pointer(x, y, buttons=0):
    return struct.pack(">BBHH", 5, buttons, x, y)


def request(incremental, width, height):
    return struct.pack(">BBHHHH", 3, incremental, 0, 0, width, height)


def test_a():
    """Announced depth against the pixel size the ZRLE encoder actually uses."""
    sock, width, height, pixel_format = handshake([16])
    bpp, depth = pixel_format[0], pixel_format[1]
    print(f"A: ServerInit says bits-per-pixel {bpp}, depth {depth}, "
          f"maxima {struct.unpack('>HHH', pixel_format[4:10])}")
    sock.sendall(request(0, width, height))
    inflater = zlib.decompressobj()
    for encoding, sizes in read_update(sock, inflater, width, height, bpp):
        if encoding == 16:
            expected = 4 if depth > 24 else 3
            print(f"A: ZRLE tiles are self-consistent only as {sizes}-byte CPIXELs; "
                  f"a client honouring depth {depth} reads {expected}-byte CPIXELs")
            sock.close()
            # The defect is the disagreement: announced depth says one size, the
            # encoder used another. Agreement means the defect is gone.
            return "PASS" if sizes != [expected] else "FAIL"
    sock.close()
    return "FAIL"


def test_b():
    """An update encoded in the old format although SetPixelFormat has arrived."""
    sock, width, height, _pf = handshake([16])
    sock.sendall(b"\x00\x00\x00\x00" + PF8)
    time.sleep(0.3)
    inflater = zlib.decompressobj()
    # Dirty the screen once and consume that update, so the next one is incremental.
    sock.sendall(pointer(40, 40, 1) + pointer(120, 90, 1) + pointer(120, 90))
    sock.sendall(request(0, width, height))
    read_update(sock, inflater, width, height, 8)
    # A request is now pending with a clean screen: nothing to send yet.
    sock.sendall(request(1, width, height))
    time.sleep(0.3)
    # One packet: dirty the screen, then change the format. A server that reads
    # everything available before encoding answers in the NEW format; one that
    # reads a single message per pass answers in the old one.
    sock.sendall(pointer(200, 150, 1) + pointer(260, 200, 1) + pointer(260, 200)
                 + b"\x00\x00\x00\x00" + PF32)
    sizes_seen = []
    sock.settimeout(3)
    for _ in range(3):
        try:
            for encoding, sizes in read_update(sock, inflater, width, height, 32):
                if encoding == 16:
                    sizes_seen.append(sizes)
        except (TimeoutError, socket.timeout, EOFError):
            break
        sock.sendall(request(1, width, height))
        sock.sendall(pointer(300, 220, 1) + pointer(340, 240, 1) + pointer(340, 240))
    sock.close()
    if not sizes_seen:
        print("B: no update arrived after the format change; inconclusive")
        return "FAIL"
    print(f"B: pixel sizes of the updates after SetPixelFormat(32-bit): {sizes_seen}")
    return "PASS" if [s for s in sizes_seen if s == [1]] else "FAIL"


if __name__ == "__main__":
    # Exit status is the verdict, one value per outcome, so the runner can require
    # exactly the one it expects: 0 both reproduced, 1 neither, 2 one of the two,
    # 3 a probe that could not run to a verdict (which must never pass as "neither").
    results = {}
    for name, test in (("A", test_a), ("B", test_b)):
        try:
            results[name] = test()
        except (SystemExit, EOFError, OSError) as error:
            # One probe failing to run must not hide the other's verdict.
            detail = error.code if isinstance(error, SystemExit) else error
            print(f"{name}: inconclusive: {detail}")
            results[name] = "INCONCLUSIVE"
    for name, verdict in results.items():
        print(f"{name}: {'REPRODUCED' if verdict == 'PASS' else 'not reproduced' if verdict == 'FAIL' else 'INCONCLUSIVE'}")
    if any(v == "INCONCLUSIVE" for v in results.values()):
        sys.exit(3)
    reproduced = sum(v == "PASS" for v in results.values())
    sys.exit(0 if reproduced == 2 else 1 if reproduced == 0 else 2)
PROBE

cat >"$work/runner.sh" <<'INNER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update >/dev/null
apt-get install -y --no-install-recommends \
  ca-certificates curl cmake ninja-build gcc make zlib1g-dev python3 patch >/dev/null
mkdir -p /build/libvnc && cd /build/libvnc
curl -fsSL -o /tmp/libvncserver.tar.gz "$TARBALL_URL"
echo "$TARBALL_SHA  /tmp/libvncserver.tar.gz" | sha256sum -c - >/dev/null
tar xzf /tmp/libvncserver.tar.gz --strip-components=1 -C /build/libvnc
echo "== upstream LibVNCServer 0.9.15, verified against its published digest =="

if [ "$WITH_FIX" = yes ]; then
  echo "== applying the two candidate fixes =="
  # A: announce the depth the caller actually described.
  python3 - <<'FIX'
import pathlib
p = pathlib.Path("/build/libvnc/src/libvncserver/main.c")
s = p.read_text()
old = "   screen->bitsPerPixel = screen->depth = 8*bytesPerPixel;"
new = ("   screen->bitsPerPixel = 8*bytesPerPixel;\n"
       "   screen->depth = bitsPerSample*samplesPerPixel;")
assert s.count(old) == 1
p.write_text(s.replace(old, new))
FIX
  # B: drain the client socket before returning to the send path.
  patch --batch --forward --fuzz=0 -p1 -d /build/libvnc \
    -i /patches/0002-drain-client-messages.patch
fi

cmake -G Ninja -S . -B out -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DBUILD_SHARED_LIBS=ON \
  -DWITH_EXAMPLES=ON -DWITH_TESTS=OFF -DWITH_GNUTLS=OFF -DWITH_OPENSSL=OFF \
  -DWITH_SDL=OFF -DWITH_GTK=OFF -DWITH_FFMPEG=OFF -DWITH_SYSTEMD=OFF \
  -DWITH_JPEG=OFF -DWITH_PNG=OFF -DCMAKE_BUILD_TYPE=Release >/dev/null
cmake --build out -j "$(nproc)" >/dev/null
server=$(find /build/libvnc/out -name example -type f -perm -u+x | head -1)
[ -n "$server" ] || { echo "examples/server/example was not built" >&2; exit 1; }

# -deferupdate 0 is the immediate-send mode: gnome-remote-desktop sets
# rfb_screen->deferUpdateTime = 0, and with it an update is encoded in the same
# event-loop pass in which the previous client message was read.
"$server" -rfbport 5999 -deferupdate 0 >/tmp/example.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 40); do
  if python3 -c 'import socket,sys; socket.create_connection(("127.0.0.1",5999),1).close()' 2>/dev/null; then
    break
  fi
  sleep 0.25
done

echo "== probing =="
set +e
python3 /probe.py
status=$?
set -e
# The probe's status is one value per outcome (0 both, 1 neither, 2 one, 3 no
# verdict); each mode accepts exactly the one it stands for.
case "$WITH_FIX/$status" in
  yes/1) echo "WITH FIXES: neither defect reproduces" ;;
  yes/0) echo "FIXES DID NOT HELP: both defects still reproduce" >&2; exit 1 ;;
  yes/2) echo "FIXES INCOMPLETE: one of the two defects still reproduces" >&2; exit 1 ;;
  no/0) echo "UPSTREAM: both defects reproduce" ;;
  no/1) echo "UPSTREAM: neither defect reproduced" >&2; exit 1 ;;
  no/2) echo "UPSTREAM: only one of the two defects reproduced" >&2; exit 1 ;;
  *) echo "the probe reached no verdict (status $status)" >&2; exit 1 ;;
esac
INNER

docker run --rm --network bridge \
  -e TARBALL_URL="$tarball_url" -e TARBALL_SHA="$tarball_sha" -e WITH_FIX="$with_fix" \
  -v "$work/runner.sh:/runner.sh:ro" -v "$work/probe.py:/probe.py:ro" \
  -v "$PWD/patches/libvncserver:/patches:ro" \
  "ubuntu:26.04@sha256:da6fc2be547864451aa253836dd926da33623312df4a9a243e35dc877c378a78" \
  bash /runner.sh
