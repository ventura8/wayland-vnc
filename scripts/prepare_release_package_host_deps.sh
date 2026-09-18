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
  # flatpak-builder cannot build until the app's runtime and SDK are present; read the
  # version from the manifest so it never drifts, and install into the --user scope the
  # builder is run with below.
  manifest=packaging/flatpak/io.github.ventura8.wayland_vnc.yaml
  rt_version=$(sed -n 's/^runtime-version: *"\{0,1\}\([0-9.]\{1,\}\)"\{0,1\} *$/\1/p' "$manifest" | head -1)
  flatpak install --user --noninteractive flathub \
    "org.gnome.Platform//${rt_version}" "org.gnome.Sdk//${rt_version}"
  ;;
snap)
  sudo snap install snapcraft --classic
  # snapcraft builds this core26 snap in an LXD container (the runner's own Ubuntu may
  # differ from core26), so LXD must be installed, initialised, and this user in the
  # lxd group before snapcraft runs. The group only takes effect in a new login, so the
  # build step enters it with `sg lxd`.
  sudo snap install lxd || sudo snap refresh lxd || true
  sudo lxd waitready --timeout=60
  sudo lxd init --auto
  sudo usermod -aG lxd "$(id -un)"
  # Docker (present on the runner) sets the FORWARD chain policy to DROP, which blocks
  # the LXD bridge's NAT so snapcraft's build container has no network. Re-allow it.
  sudo iptables -P FORWARD ACCEPT || true
  ;;
*)
  echo "Unknown package kind: $kind" >&2
  exit 2
  ;;
esac
