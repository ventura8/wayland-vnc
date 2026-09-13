#!/usr/bin/env bash
# Build wayland-vnc-grd and exercise install / remove / purge in a throwaway Ubuntu
# container: the declared dependencies must satisfy the binaries' links, the daemon
# must run, the drop-in must land where systemd's user instance reads it, and a purge
# must leave nothing behind. Never touches the host.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

artifacts_dir=${WAYLAND_VNC_GRD_ARTIFACTS_DIR:-artifacts/grd-deb}
mkdir -p reports/distro-logs
bash scripts/build_grd_package.sh "$artifacts_dir" >/dev/null
# Absolute for the bind mount, the same way the build script resolved it.
artifacts_dir=$(realpath -- "$artifacts_dir")

runner=$(mktemp)
trap 'rm -f "$runner"' EXIT
cat >"$runner" <<'INNER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update >/dev/null
deb=$(ls /pkg/wayland-vnc-grd_*_"$(dpkg --print-architecture)".deb | head -1)
echo "== happy: install resolves every declared dependency =="
apt-get install -y "$deb" >/dev/null 2>&1
daemon=/opt/wayland-vnc/grd/libexec/gnome-remote-desktop-daemon
test -x "$daemon" || { echo "daemon missing after install" >&2; exit 1; }
if ldd "$daemon" | grep -q "not found"; then
  echo "unresolved libraries after install:" >&2; ldd "$daemon" | grep "not found" >&2; exit 1
fi
ldd "$daemon" | grep -q "/opt/wayland-vnc/libvnc/lib/libvncserver.so.1" \
  || { echo "daemon does not link the private LibVNCServer" >&2; exit 1; }
echo "  ok: links resolve, private LibVNCServer in use"
version=$("$daemon" --version 2>&1 | head -1)
case "$version" in *50.2*) echo "  ok: daemon runs ($version)" ;; *) echo "daemon --version said: $version" >&2; exit 1 ;; esac
dropin=/usr/lib/systemd/user/gnome-remote-desktop.service.d/20-wayland-vnc-private-daemon.conf
test -f "$dropin" || { echo "drop-in missing" >&2; exit 1; }
grep -q "^ExecStart=$daemon --vnc-port 5900" "$dropin" || { echo "drop-in does not run the private daemon" >&2; exit 1; }
echo "  ok: user drop-in installed"
test -f /usr/share/doc/wayland-vnc-grd/copyright || { echo "copyright missing" >&2; exit 1; }
dpkg -s gnome-remote-desktop >/dev/null || { echo "distribution daemon not kept" >&2; exit 1; }
echo "  ok: distribution package still installed alongside"

echo "== bad: dpkg refuses a truncated package =="
head -c 200 "$deb" > /tmp/broken.deb
if dpkg -i /tmp/broken.deb >/dev/null 2>&1; then echo "dpkg accepted a truncated .deb" >&2; exit 1; fi
echo "  ok: truncated .deb rejected"

echo "== purge removes everything =="
apt-get purge -y wayland-vnc-grd >/dev/null 2>&1
test ! -e /opt/wayland-vnc/grd || { echo "/opt/wayland-vnc/grd left after purge" >&2; exit 1; }
test ! -e /opt/wayland-vnc/libvnc || { echo "/opt/wayland-vnc/libvnc left after purge" >&2; exit 1; }
test ! -e "$dropin" || { echo "drop-in left after purge" >&2; exit 1; }
echo "  ok: purge left nothing behind"
echo "GRD DEB SMOKE PASSED"
INNER

echo "=== grd deb smoke: ubuntu:26.04 ==="
docker run --rm --network bridge -v "$artifacts_dir:/pkg:ro" -v "$runner:/runner.sh:ro" \
  "ubuntu:26.04@sha256:cd21a4f68a617580279d4b091cb18e3af9fa8a87500665f0ae5f7f757d17d367" \
  bash /runner.sh 2>&1 | tee reports/distro-logs/grd-deb-smoke-ubuntu-26.04.log
