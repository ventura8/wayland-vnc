#!/usr/bin/env bash
# Install the host tools needed to build one release package kind. Used by the
# release workflow before scripts/build_release_package.sh KIND.
set -euo pipefail
kind=${1:?"usage: prepare_release_package_host_deps.sh KIND"}

apt_install() {
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

case "$kind" in
deb)
  # build_deb runs dpkg-buildpackage inside an ubuntu:26.04 container and installs
  # its toolchain there, so the host needs docker, not debhelper.
  command -v docker >/dev/null || apt_install docker.io
  ;;
rpm)
  # Likewise build_rpm runs rpmbuild inside a fedora:44 container.
  command -v docker >/dev/null || apt_install docker.io
  ;;
arch)
  # Arch packages build inside an archlinux container in build_release_package.sh.
  command -v docker >/dev/null || apt_install docker.io
  ;;
grd-deb)
  # The private GNOME Remote Desktop builds in docker/Dockerfile.gnome's build stage.
  command -v docker >/dev/null || apt_install docker.io
  ;;
appimage)
  apt_install wget file fuse3 python3
  ;;
flatpak)
  apt_install flatpak flatpak-builder
  flatpak remote-add --if-not-exists --user flathub \
    https://flathub.org/repo/flathub.flatpakrepo || true
  ;;
snap)
  sudo snap install snapcraft --classic
  ;;
*)
  echo "Unknown package kind: $kind" >&2
  exit 2
  ;;
esac
