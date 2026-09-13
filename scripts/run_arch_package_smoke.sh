#!/usr/bin/env bash
# Build the wayland-vnc Arch package with makepkg (as a non-root user) and exercise
# install / reinstall / remove and the happy+bad scenario suite in a throwaway Arch
# container. Never touches the host.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

image=${1:-archlinux:latest}
mkdir -p artifacts/arch reports/distro-logs

runner=$(mktemp)
trap 'rm -f "$runner"' EXIT
cat >"$runner" <<'INNER'
set -euo pipefail
pacman -Sy --noconfirm >/dev/null 2>&1
pacman -S --noconfirm --needed fakeroot binutils gcc make python openssl >/dev/null 2>&1
version=$(tr -d '[:space:]' < /src/VERSION)

# makepkg refuses to run as root: build as a dedicated unprivileged user.
useradd -m builder 2>/dev/null || true
install -d -o builder -g builder /home/builder/build/repo
cp -a /src/. /home/builder/build/repo/
cp /src/packaging/arch/PKGBUILD /home/builder/build/PKGBUILD
chown -R builder:builder /home/builder/build

echo "== build =="
# stdout is noise, but stderr is the only place a makepkg failure explains itself.
su - builder -c "cd ~/build && sed -i 's/^pkgver=.*/pkgver=$version/' PKGBUILD && makepkg -f --nodeps" >/dev/null
# -maxdepth 1: makepkg drops the package here, while the repo copy underneath may
# carry a stale artifacts/arch/*.pkg.tar.zst from an earlier run on the developer's
# machine. Recursing found that one first and smoke-tested a package built from
# older sources, which CI never reproduced because it has no artifacts/ directory.
pkg=$(find /home/builder/build -maxdepth 1 -name 'wayland-vnc-*.pkg.tar.*' -print -quit)
[ -n "$pkg" ] || { echo "makepkg produced no wayland-vnc-*.pkg.tar.* in /home/builder/build" >&2; exit 1; }
cp "$pkg" /out/ 2>/dev/null || true
echo "built $(basename "$pkg")"

echo "== happy: install =="
pacman -U --noconfirm --nodeps "$pkg" >/dev/null 2>&1
command -v wayland-vnc >/dev/null || { echo "CLI missing after install" >&2; exit 1; }
test -f /usr/lib/systemd/user/wayland-vnc.service || { echo "unit missing after install" >&2; exit 1; }

echo "== scenarios =="
WAYLAND_VNC_CLI=wayland-vnc runuser -u builder -- bash /home/builder/build/repo/scripts/package_smoke_scenarios.sh

echo "== reinstall is idempotent =="
pacman -U --noconfirm --nodeps "$pkg" >/dev/null 2>&1

echo "== bad: reject a truncated package =="
head -c 200 "$pkg" > /tmp/broken.pkg.tar.zst
if pacman -U --noconfirm /tmp/broken.pkg.tar.zst >/dev/null 2>&1; then
  echo "pacman accepted a truncated package" >&2; exit 1
fi
echo "  ok: truncated package rejected"

echo "== remove leaves nothing =="
pacman -Rns --noconfirm wayland-vnc >/dev/null 2>&1
test ! -e /usr/bin/wayland-vnc || { echo "CLI left after remove" >&2; exit 1; }
test ! -d /usr/lib/wayland-vnc || { echo "module tree left after remove" >&2; exit 1; }
echo "  ok: remove left nothing behind"
echo "ARCH SMOKE PASSED"
INNER

echo "=== arch smoke: $image ==="
docker run --rm --network bridge \
  -v "$PWD:/src:ro" -v "$runner:/runner.sh:ro" -v "$PWD/artifacts/arch:/out" \
  "$image" bash /runner.sh \
  2>&1 | tee "reports/distro-logs/arch-smoke.log"
