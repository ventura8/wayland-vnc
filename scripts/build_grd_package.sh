#!/usr/bin/env bash
# Build wayland-vnc-grd: the project's private GNOME Remote Desktop build as a .deb.
#
# GNOME desktops are served through gnome-remote-desktop's VNC backend, and Ubuntu's
# own build of it segfaults whenever a client disconnects, announces the wrong colour
# depth and strands clients in its connection queue (packaging plan, section 16). The
# build under docker/Dockerfile.gnome carries the project's patch series against
# checksum-pinned upstream sources; this script packages exactly that build:
#   /opt/wayland-vnc/grd, /opt/wayland-vnc/libvnc          the daemon and its LibVNCServer
#   /usr/lib/systemd/user/gnome-remote-desktop.service.d/  a drop-in that runs it in
#                                                          place of the distribution daemon
# The distribution package stays installed and untouched; removing wayland-vnc-grd
# puts it back. Library dependencies are read off the binaries with ldd inside the
# build container and mapped to Ubuntu packages, so the .deb declares what it links.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

version=$(tr -d '[:space:]' <VERSION)
out=${1:-artifacts/grd-deb}
mkdir -p "$out"
# Resolved after creation so an absolute argument is honoured: the bind mount used to
# be "$PWD/$out", which turned /tmp/x into <repo>/tmp/x and wrote the package where
# nobody was looking for it.
out=$(realpath -- "$out")

# One architecture per run, spelled out (scripts/target-arch.sh: the host's unless
# WAYLAND_VNC_ARCH says otherwise), and asked for explicitly on every docker call so
# the compiled tree, the control file and the file name can never disagree.
arch=$(bash scripts/target-arch.sh deb)
platform=$(bash scripts/target-arch.sh platform)
image="wayland-vnc-grd-build:dev-$arch"
# Building for the other architecture runs the toolchain under qemu-user; the
# image is told so, because one sanitizer check cannot run there (Dockerfile.gnome).
emulated=0
host_arch=$(env -u WAYLAND_VNC_ARCH -u DOCKER_DEFAULT_PLATFORM bash scripts/target-arch.sh deb)
[ "$arch" = "$host_arch" ] || emulated=1

echo "== building the private daemon for $arch (docker/Dockerfile.gnome, stage grd-build) =="
docker build -q --platform "$platform" --target grd-build \
  --build-arg "WAYLAND_VNC_EMULATED=$emulated" \
  -f docker/Dockerfile.gnome -t "$image" . >/dev/null

runner=$(mktemp)
trap 'rm -f "$runner"' EXIT
cat >"$runner" <<'INNER'
set -euo pipefail
version=$1
arch=$2
pkg=/build/wayland-vnc-grd
rm -rf "$pkg"
install -d -m 755 "$pkg/DEBIAN" "$pkg/opt/wayland-vnc" \
  "$pkg/usr/lib/systemd/user/gnome-remote-desktop.service.d" \
  "$pkg/usr/share/doc/wayland-vnc-grd"
cp -a /opt/wayland-vnc/grd "$pkg/opt/wayland-vnc/grd"
# Only the shared library is needed at run time; headers, cmake and pkg-config files
# are build-time artefacts of the private tree.
install -d -m 755 "$pkg/opt/wayland-vnc/libvnc/lib"
cp -a /opt/wayland-vnc/libvnc/lib/libvncserver.so* "$pkg/opt/wayland-vnc/libvnc/lib/"
strip --strip-unneeded "$pkg/opt/wayland-vnc/grd/libexec/gnome-remote-desktop-daemon" \
  "$pkg/opt/wayland-vnc/grd/bin/grdctl" "$pkg/opt/wayland-vnc/libvnc/lib/"libvncserver.so.*.*.* 2>/dev/null || true

echo "== library dependencies from the binaries themselves =="
depends=$(
  for bin in /opt/wayland-vnc/grd/libexec/gnome-remote-desktop-daemon /opt/wayland-vnc/libvnc/lib/libvncserver.so.1; do
    ldd "$bin" | awk '/=> \//{print $3}'
  done | sort -u | grep -v '^/opt/wayland-vnc/' | while read -r lib; do
    dpkg -S "$(readlink -f "$lib")" 2>/dev/null | cut -d: -f1
  done | sort -u | paste -sd, - | sed 's/,/, /g'
)
[ -n "$depends" ] || { echo "no library dependencies resolved; refusing to build an undeclared package" >&2; exit 1; }
echo "  $depends"

cat >"$pkg/DEBIAN/control" <<CTRL
Package: wayland-vnc-grd
Version: $version
Section: admin
Priority: optional
Architecture: $arch
Maintainer: ventura8 <alexandrescu.sergiu@gmail.com>
Depends: $depends, gnome-remote-desktop
Enhances: wayland-vnc
Homepage: https://github.com/ventura8/wayland-vnc
Description: GNOME Remote Desktop VNC backend, patched, for wayland-vnc
 The project's private build of GNOME Remote Desktop 50.2 and LibVNCServer 0.9.15
 with the wayland-vnc patch series: no crash when a client disconnects, the colour
 depth the encoder really uses, hung-up clients dropped from the connection queue,
 and every client message processed before an update is sent. Installed under
 /opt/wayland-vnc and run in place of the distribution daemon through a systemd
 user drop-in; the distribution package stays installed, and removing this one
 puts it back.
CTRL

cat >"$pkg/usr/lib/systemd/user/gnome-remote-desktop.service.d/20-wayland-vnc-private-daemon.conf" <<'CONF'
# Installed by wayland-vnc-grd. The distribution's VNC-enabled gnome-remote-desktop
# segfaults whenever a client disconnects (rfb_client is used after clientGone cleared
# it); this runs the project's patched build from /opt/wayland-vnc instead. Removing
# the wayland-vnc-grd package removes this file and the distribution daemon is used
# again after the next daemon-reload.
[Service]
ExecStart=
ExecStart=/opt/wayland-vnc/grd/libexec/gnome-remote-desktop-daemon --vnc-port 5900
Environment=WAYLAND_VNC_ENABLE_DMABUF=0
Environment=WAYLAND_VNC_ENABLE_CLIPBOARD=0
CONF

cat >"$pkg/usr/share/doc/wayland-vnc-grd/README" <<'DOC'
wayland-vnc-grd runs the project's patched GNOME Remote Desktop in place of the
distribution daemon, through a systemd user drop-in. It takes effect for a user's
session after `systemctl --user daemon-reload && systemctl --user restart
gnome-remote-desktop.service`, which the package does for logged-in users at
install and removal, or at the next login. Sources, patches and the build
recipe: https://github.com/ventura8/wayland-vnc (patches/, docker/Dockerfile.gnome).
DOC
cp /build/copyright "$pkg/usr/share/doc/wayland-vnc-grd/copyright"
cp /build/postinst "$pkg/DEBIAN/postinst"; cp /build/postrm "$pkg/DEBIAN/postrm"
chmod 755 "$pkg/DEBIAN/postinst" "$pkg/DEBIAN/postrm"
find "$pkg/opt" -type d -exec chmod 755 {} +
dpkg-deb --root-owner-group --build "$pkg" "/out/wayland-vnc-grd_${version}_${arch}.deb" >/dev/null
dpkg-deb --info "/out/wayland-vnc-grd_${version}_${arch}.deb" | sed -n '/Package/,/Description/p'
INNER

docker run --rm --platform "$platform" -v "$runner:/r.sh:ro" -v "$out:/out" \
  -v "$PWD/packaging/grd/postinst:/build/postinst:ro" -v "$PWD/packaging/grd/postrm:/build/postrm:ro" \
  -v "$PWD/packaging/grd/copyright:/build/copyright:ro" \
  "$image" bash /r.sh "$version" "$arch"
echo "built $out/wayland-vnc-grd_${version}_${arch}.deb"
