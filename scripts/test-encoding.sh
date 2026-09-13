#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ $# != 2 ]]; then
  echo "Usage: $0 patched-libvnc-source libvnc-build-dir" >&2
  exit 2
fi
source_dir=$(realpath -- "$1")
build_dir=$(realpath -- "$2")
mkdir -p artifacts/native
# Under the same sanitizers as the rest of the native gate: the encoding parser is
# the code the ZRLE-preference patch touches, so it is the test that most needs them.
cc -std=c11 -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer -g \
  -I "$source_dir/include" -I "$build_dir/include" tests/native/test-encoding.c \
  -L "$build_dir" -Wl,-rpath,"$build_dir" -lvncserver -o artifacts/native/test-encoding
# Leak detection is on, except under user-mode emulation (an arm64 build on an x86
# host): LeakSanitizer stops with "encountered a fatal error" there, because it
# relies on ptrace-style thread suspension that qemu-user does not provide. The
# build passes WAYLAND_VNC_EMULATED=1 in exactly that case and nowhere else; a
# native run, on either architecture, keeps the check.
leaks=1
if [[ "${WAYLAND_VNC_EMULATED:-0}" == 1 ]]; then
  echo "test-encoding: user-mode emulation; LeakSanitizer disabled for this run" >&2
  leaks=0
fi
ASAN_OPTIONS="detect_leaks=$leaks" timeout 20 artifacts/native/test-encoding
