#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
mkdir -p artifacts/native
cc -std=c11 -Wall -Wextra -Werror -Wconversion -fsanitize=address,undefined \
  -fno-omit-frame-pointer -g -I native tests/native/test-buffer-layout.c \
  -o artifacts/native/test-buffer-layout
ASAN_OPTIONS=detect_leaks=1 artifacts/native/test-buffer-layout
