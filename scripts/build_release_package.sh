#!/usr/bin/env bash
# Build one release package kind into artifacts/<kind>/. Version comes from the
# repo-root VERSION. Native kinds (deb/rpm/arch) build in the matching container so a
# release runner needs no distro-specific host toolchain; portable kinds build on host.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH
kind=${1:?"usage: build_release_package.sh KIND"}
# Validated BEFORE anything is created or removed: a typo used to create (and would
# now delete) artifacts/<typo> before the dispatch below rejected it.
case "$kind" in
deb | grd-deb | rpm | arch | appimage | flatpak | snap) ;;
*)
  echo "Unknown package kind: $kind" >&2
  exit 2
  ;;
esac
version=$(tr -d '[:space:]' <VERSION)
out="artifacts/$kind"
# Start empty. A package left by an earlier run is otherwise published alongside the
# new one, which is how a stale build reaches a release; the Arch driver already
# carried a workaround for exactly that.
rm -rf -- "$out"
mkdir -p "$out"

build_deb() {
  local runner
  runner=$(mktemp)
  cat >"$runner" <<'INNER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update >/dev/null
apt-get install -y --no-install-recommends build-essential debhelper dpkg-dev python3 >/dev/null
mkdir -p /build && cp -a /src/. /build/ && cd /build
rm -rf debian/wayland-vnc debian/.debhelper debian/files ../wayland-vnc_*.deb 2>/dev/null || true
DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -b -us -uc >/dev/null
cp ../wayland-vnc_*_all.deb /out/
INNER
  docker run --rm -v "$PWD:/src:ro" -v "$runner:/r.sh:ro" -v "$PWD/$out:/out" \
    ubuntu:26.04 bash /r.sh
  rm -f "$runner"
}

build_grd_deb() {
  bash scripts/build_grd_package.sh "$out"
}

build_rpm() {
  local runner
  runner=$(mktemp)
  cat >"$runner" <<'INNER'
set -euo pipefail
dnf install -y rpm-build python3 >/dev/null 2>&1
version=$(tr -d '[:space:]' < /src/VERSION)
top=/root/rpmbuild; mkdir -p "$top"/{BUILD,RPMS,SOURCES,SPECS,BUILDROOT}
cp -a /src "$top/SOURCES/repo"
cp /src/packaging/rpm/wayland-vnc.spec "$top/SPECS/"
rpmbuild --define "_topdir $top" --define "vnc_version $version" -bb "$top/SPECS/wayland-vnc.spec" >/dev/null
find "$top/RPMS" -name '*.rpm' -exec cp {} /out/ \;
INNER
  docker run --rm -v "$PWD:/src:ro" -v "$runner:/r.sh:ro" -v "$PWD/$out:/out" \
    fedora:44 bash /r.sh
  rm -f "$runner"
}

build_arch() {
  local runner
  runner=$(mktemp)
  cat >"$runner" <<'INNER'
set -euo pipefail
pacman -Sy --noconfirm >/dev/null 2>&1
pacman -S --noconfirm --needed fakeroot binutils gcc make python >/dev/null 2>&1
version=$(tr -d '[:space:]' < /src/VERSION)
useradd -m builder 2>/dev/null || true
install -d -o builder -g builder /home/builder/build/repo
cp -a /src/. /home/builder/build/repo/
cp /src/packaging/arch/PKGBUILD /home/builder/build/PKGBUILD
chown -R builder:builder /home/builder/build
su - builder -c "cd ~/build && sed -i 's/^pkgver=.*/pkgver=$version/' PKGBUILD && makepkg -f --nodeps" >/dev/null 2>&1
# -maxdepth 1: makepkg drops the package here, while the repo copy underneath may
# carry a stale artifacts/arch/*.pkg.tar.zst from an earlier run, which would be
# published over the freshly built one.
find /home/builder/build -maxdepth 1 -name '*.pkg.tar.*' -exec cp {} /out/ \;
INNER
  docker run --rm -v "$PWD:/src:ro" -v "$runner:/r.sh:ro" -v "$PWD/$out:/out" \
    archlinux:latest bash /r.sh
  rm -f "$runner"
}

build_appimage() {
  local appdir="packaging/appimage/AppDir"
  rm -rf "$appdir"
  WAYLAND_VNC_REPO_ROOT="$PWD" packaging/stage-payload.sh "$appdir" /usr
  install -m 755 packaging/appimage/AppRun "$appdir/AppRun"
  install -m 644 packaging/appimage/wayland-vnc.desktop "$appdir/wayland-vnc.desktop"
  install -m 644 packaging/appimage/wayland-vnc.png "$appdir/wayland-vnc.png"
  # The AppImage runtime is architecture-specific, so the tool (and the runtime it
  # embeds) follows scripts/target-arch.sh: the host's architecture, or the one an
  # emulated run asks for. Both builds of the tool are pinned to a tagged release and
  # verified before ever being made executable. The previous URL was the mutable
  # `continuous` tag fetched without a checksum, so whatever that tag pointed at on
  # the day was executed with the privileges of the release build, and every AppImage
  # it produced depended on it.
  local arch tool tool_sha256
  arch=$(bash scripts/target-arch.sh appimage)
  tool="packaging/appimage/appimagetool-$arch"
  local tool_version=1.9.1
  case "$arch" in
  x86_64) tool_sha256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0 ;;
  aarch64) tool_sha256=f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158 ;;
  *)
    echo "no pinned appimagetool checksum for architecture '$arch'" >&2
    return 1
    ;;
  esac
  if [[ ! -x "$tool" ]]; then
    wget -qO "$tool.download" \
      "https://github.com/AppImage/appimagetool/releases/download/${tool_version}/appimagetool-${arch}.AppImage"
    mv -- "$tool.download" "$tool"
    chmod +x "$tool"
  fi
  # Verify on EVERY run, not only after a download: the cached copy under
  # packaging/appimage/ survives between builds and is executed with the privileges of
  # the release build, so a tampered or half-written one must never reach exec.
  if ! printf '%s  %s\n' "$tool_sha256" "$tool" | sha256sum -c - >/dev/null 2>&1; then
    rm -f -- "$tool" "$tool.download"
    echo "appimagetool ${tool_version} failed its checksum; refusing to run it" >&2
    return 1
  fi
  ARCH="$arch" "$tool" --appimage-extract-and-run \
    "$appdir" "$out/wayland-vnc-${version}-${arch}.AppImage"
}

build_flatpak() {
  # --user: the runtime and SDK were installed into the user scope by the prepare step.
  flatpak-builder --user --force-clean --repo=packaging/flatpak/repo \
    packaging/flatpak/builddir packaging/flatpak/io.github.ventura8.wayland_vnc.yaml
  flatpak build-bundle packaging/flatpak/repo \
    "$out/wayland-vnc-${version}.flatpak" io.github.ventura8.wayland_vnc
}

build_snap() {
  local snap="wayland-vnc_${version}.snap"
  rm -f -- "$snap"
  # snapcraft mounts the project tree (the one holding snap/snapcraft.yaml) into its
  # LXD build container, so it must be the repo root for the part's `source: .` to be
  # the whole repo. Run from packaging/snap instead and "source: ../.." escaped the
  # mount, so snapcraft tried to copy the container's / (PermissionError on /sys).
  # Stage the manifest at <root>/snap for this build; `sg lxd` enters the lxd group
  # the prepare step added so snapcraft can drive its LXD backend.
  install -D -m 644 packaging/snap/snapcraft.yaml snap/snapcraft.yaml
  sg lxd -c "snapcraft --output '$snap'"
  rm -rf -- snap
  cp -- "$snap" "$out/"
}

case "$kind" in
grd-deb)
  build_grd_deb
  ;;
deb) build_deb ;;
rpm) build_rpm ;;
arch) build_arch ;;
appimage) build_appimage ;;
flatpak) build_flatpak ;;
snap) build_snap ;;
*)
  echo "Unknown package kind: $kind" >&2
  exit 2
  ;;
esac

echo "built $kind artifacts:"
ls -la "$out"
