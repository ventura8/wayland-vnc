#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH
unit_root=$(mktemp -d /tmp/wayland-vnc-units-XXXXXX)
trap 'rm -rf -- "$unit_root"' EXIT
for backend in grd w0vncserver wayvnc; do
  backend_root="$unit_root/$backend"
  PYTHONPATH=src python3 -m wayland_vnc setup \
    --backend "$backend" --staging-root "$backend_root" --json >/dev/null
  executable=$(sed -n 's/^ExecStart=\([^ ]*\).*/\1/p' \
    "$backend_root/usr/lib/systemd/user/wayland-vnc.service")
  mkdir -p -- "$backend_root$(dirname -- "$executable")"
  touch -- "$backend_root$executable"
  chmod 755 -- "$backend_root$executable"
  mkdir -p -- "$backend_root/usr/lib/systemd/system"
  for target in sysinit.target basic.target graphical-session.target; do
    printf '[Unit]\nDescription=Test fixture %s\n' "$target" \
      >"$backend_root/usr/lib/systemd/system/$target"
  done
  cp -- "$backend_root/usr/lib/systemd/user/wayland-vnc.service" \
    "$backend_root/usr/lib/systemd/system/wayland-vnc.service"
  systemd-analyze --root="$backend_root" verify wayland-vnc.service
  PYTHONPATH=src python3 -m wayland_vnc uninstall \
    --staging-root "$backend_root" --json >/dev/null
  rm -f -- "$backend_root$executable"
  rm -rf -- "$backend_root/usr/lib/systemd/system"
done
# -print -quit: the first leftover is enough, and nothing downstream of find can be
# closed early under pipefail.
if [ -n "$(find "$unit_root" \( -type f -o -type l \) -print -quit)" ]; then
  echo "Staging round trip left files behind" >&2
  exit 1
fi
