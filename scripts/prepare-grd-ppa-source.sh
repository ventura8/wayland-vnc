#!/usr/bin/env bash
# Assemble the wayland-vnc-grd Debian source tree for a PPA upload.
#
#   scripts/prepare-grd-ppa-source.sh OUTDIR PPA_VERSION DISTRO
#
# Launchpad builds source packages offline, so the checksum-pinned upstream tarballs
# (sources.json) are fetched HERE, verified, and vendored into the tree; the tree's
# debian/rules then builds GNOME Remote Desktop and LibVNCServer from them with the
# project's patch series. The caller runs dpkg-buildpackage -S in the printed
# directory. Maintainer and the changelog's qualification line come from the
# environment: MAINTAINER_NAME, MAINTAINER_EMAIL, WAYLAND_VNC_PPA_STATUS.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"

outdir=${1:?"usage: prepare-grd-ppa-source.sh OUTDIR PPA_VERSION DISTRO"}
ppa_version=${2:?"usage: prepare-grd-ppa-source.sh OUTDIR PPA_VERSION DISTRO"}
distro=${3:?"usage: prepare-grd-ppa-source.sh OUTDIR PPA_VERSION DISTRO"}
maintainer_name=${MAINTAINER_NAME:?"MAINTAINER_NAME is required"}
maintainer_email=${MAINTAINER_EMAIL:?"MAINTAINER_EMAIL is required"}
status=${WAYLAND_VNC_PPA_STATUS:-"Not release-qualified: no qualification evidence for this commit"}

tree="$outdir/wayland-vnc-grd-$ppa_version"
rm -rf "$tree"
install -d -m 755 "$tree/debian/source" "$tree/vendor" "$tree/extra" \
  "$tree/patches/gnome-remote-desktop" "$tree/patches/libvncserver"

# Upstream tarballs, named as debian/rules expects, verified against sources.json.
fetch() {
  local name=$1 file=$2 url sha
  read -r url sha < <(
    python3 - "$name" <<'PY'
import json, sys
name = sys.argv[1]
for source in json.load(open("sources.json"))["sources"]:
    if source["name"] == name:
        print(source["url"], source["sha256"])
        break
else:
    raise SystemExit(f"{name} is not pinned in sources.json")
PY
  )
  curl -fsSL -o "$tree/vendor/$file" "$url"
  printf '%s  %s\n' "$sha" "$tree/vendor/$file" | sha256sum -c - >/dev/null
  echo "  vendored $file ($sha)"
}
echo "== vendoring pinned upstream sources =="
fetch gnome-remote-desktop gnome-remote-desktop-50.2.tar.xz
fetch libvncserver LibVNCServer-0.9.15.tar.gz

echo "== packaging files =="
cp packaging/grd/debian/control packaging/grd/debian/rules "$tree/debian/"
cp packaging/grd/debian/source/format "$tree/debian/source/format"
cp packaging/grd/copyright "$tree/debian/copyright"
install -m 755 packaging/grd/postinst "$tree/debian/wayland-vnc-grd.postinst"
install -m 755 packaging/grd/postrm "$tree/debian/wayland-vnc-grd.postrm"
cp patches/gnome-remote-desktop/*.patch "$tree/patches/gnome-remote-desktop/"
cp patches/libvncserver/*.patch "$tree/patches/libvncserver/"
cp packaging/grd/private-daemon.conf packaging/grd/README "$tree/extra/"

cat >"$tree/debian/changelog" <<CHANGELOG
wayland-vnc-grd (${ppa_version}) ${distro}; urgency=medium

  * Patched GNOME Remote Desktop 50.2 + LibVNCServer 0.9.15 for wayland-vnc
  * ${status}
  * See https://github.com/ventura8/wayland-vnc/releases

 -- ${maintainer_name} <${maintainer_email}>  $(date -R)
CHANGELOG

echo "$tree"
