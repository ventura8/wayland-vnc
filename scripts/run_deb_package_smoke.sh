#!/usr/bin/env bash
# Build the wayland-vnc .deb and exercise install / reinstall / remove / purge and the
# happy+bad-path scenario suite in throwaway Debian-family containers. Never touches
# the host: everything happens inside `docker run --rm`.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH

images=("${@:-}")
if [[ -z "${images[0]:-}" ]]; then
  images=(ubuntu:26.04 debian:trixie)
fi
artifacts_dir=${WAYLAND_VNC_DEB_ARTIFACTS_DIR:-artifacts/deb}
mkdir -p "$artifacts_dir" reports/distro-logs
# Absolute for the bind mount: "$PWD/$artifacts_dir" would turn an absolute override
# into a path under the checkout.
artifacts_dir=$(realpath -- "$artifacts_dir")

runner=$(mktemp)
trap 'rm -f "$runner"' EXIT
cat >"$runner" <<'INNER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update >/dev/null
apt-get install -y --no-install-recommends \
  build-essential debhelper dpkg-dev python3 python3-pil systemd >/dev/null 2>&1
mkdir -p /build && cp -a /src/. /build/ && cd /build
rm -rf debian/wayland-vnc debian/.debhelper debian/files ../wayland-vnc_*.deb obj-* 2>/dev/null || true
echo "== build =="
DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -b -us -uc >/dev/null 2>&1
deb=$(ls ../wayland-vnc_*_all.deb | head -1)
cp "$deb" /out/ 2>/dev/null || true
echo "built $(basename "$deb")"

echo "== happy: install (pulls wayvnc/openssl deps) =="
apt-get install -y "$deb" >/dev/null 2>&1
command -v wayland-vnc >/dev/null || { echo "CLI missing after install" >&2; exit 1; }
test -f /usr/lib/systemd/user/wayland-vnc.service || { echo "user unit missing" >&2; exit 1; }
echo "== scenarios =="
WAYLAND_VNC_CLI=wayland-vnc bash /build/scripts/package_smoke_scenarios.sh

echo "== reinstall is idempotent =="
apt-get install -y --reinstall "$deb" >/dev/null 2>&1
command -v wayland-vnc >/dev/null || { echo "CLI missing after reinstall" >&2; exit 1; }

echo "== bad: dpkg refuses a truncated package =="
head -c 200 "$deb" > /tmp/broken.deb
if dpkg -i /tmp/broken.deb >/dev/null 2>&1; then echo "dpkg accepted a truncated .deb" >&2; exit 1; fi
echo "  ok: truncated .deb rejected"

echo "== purge removes everything =="
apt-get purge -y wayland-vnc >/dev/null 2>&1
test ! -e /usr/bin/wayland-vnc || { echo "CLI left after purge" >&2; exit 1; }
test ! -e /usr/lib/systemd/user/wayland-vnc.service || { echo "unit left after purge" >&2; exit 1; }
test ! -d /usr/lib/wayland-vnc || { echo "module tree left after purge" >&2; exit 1; }
echo "  ok: purge left nothing behind"

echo "== purge is idempotent =="
apt-get purge -y wayland-vnc >/dev/null 2>&1 || true
echo "DEB SMOKE PASSED"
INNER

status=0
for image in "${images[@]}"; do
  slug=${image//[:\/]/-}
  echo "=== deb smoke: $image ==="
  digest_image="$image"
  if [[ "$image" = "ubuntu:26.04" ]]; then
    digest_image="ubuntu:26.04@sha256:da6fc2be547864451aa253836dd926da33623312df4a9a243e35dc877c378a78"
  fi
  if ! docker run --rm --network bridge \
    -v "$PWD:/src:ro" -v "$runner:/runner.sh:ro" \
    -v "$artifacts_dir:/out" \
    "$digest_image" bash /runner.sh \
    2>&1 | tee "reports/distro-logs/deb-smoke-${slug}.log"; then
    status=1
    echo "deb smoke failed on $image" >&2
  fi
done
exit "$status"
