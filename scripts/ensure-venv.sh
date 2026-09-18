#!/usr/bin/env bash
# Create or refresh the project's Python virtual environment and print its bin directory.
#
# Every Python the project runs by hand or from a script -- the unit suite, the lints,
# the qualification runners, the release gate, the hardware validation, debugging
# tools -- runs from this environment (AGENTS.md, "Python runs in a virtual
# environment"). Nothing is ever pip-installed into the system interpreter, not with
# --user and not with --break-system-packages: Ubuntu marks its interpreter externally
# managed (PEP 668), lab machines differ from the laptop, and the pins in
# requirements-dev.txt are the toolchain CI uses.
#
# --system-site-packages: PyGObject and the GTK4/libadwaita typelibs are distribution
# packages the settings-app tests import; a venv that could not see them would skip
# tests that the desktop install would run.
#
#   scripts/ensure-venv.sh                # creates .venv if needed, updates pins, prints .venv/bin
#   export PATH="$(scripts/ensure-venv.sh):$PATH"
#   WAYLAND_VNC_VENV=/elsewhere scripts/ensure-venv.sh
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

venv=${WAYLAND_VNC_VENV:-$PWD/.venv}
requirements="requirements-dev.txt"
stamp="$venv/.requirements-dev.sha256"
want=$(sha256sum "$requirements" | cut -c1-64)

# A venv whose base interpreter was upgraded away has a bin/python that no longer runs;
# rebuild it rather than fail on the first import.
if [ -e "$venv/bin/python" ] && ! "$venv/bin/python" -c 'import sys' >/dev/null 2>&1; then
  echo "rebuilding $venv: its interpreter no longer runs" >&2
  rm -rf "$venv"
fi
if [ ! -x "$venv/bin/python" ]; then
  python3 -m venv --system-site-packages "$venv"
fi
# Debian and Ubuntu ship the interpreter without ensurepip unless python3-venv is
# installed, and `python3 -m venv` then produces an environment with no pip in it.
# Say what is missing rather than fail inside pip on every later step.
if ! "$venv/bin/python" -m pip --version >/dev/null 2>&1; then
  "$venv/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || {
    echo "$venv has no pip and ensurepip is unavailable: install python3-venv" \
      "(Debian/Ubuntu) or python3-pip and rerun" >&2
    rm -rf "$venv"
    exit 1
  }
fi
if [ "$(cat "$stamp" 2>/dev/null || true)" != "$want" ]; then
  "$venv/bin/python" -m pip install --quiet --require-virtualenv -r "$requirements" >&2
  printf '%s\n' "$want" >"$stamp"
fi
echo "$venv/bin"
