#!/usr/bin/env bash
# End-to-end test of the packaged GTK settings app on every supported platform.
#
# For each distro image it installs that platform's GTK4/libadwaita stack, stages the
# payload, checks the installed launcher and desktop entry, and drives the packaged app
# through happy and bad scenarios against a real filesystem and a fake systemctl.
# A final no-GTK container proves the optional GUI fails closed with an actionable
# install hint rather than an import traceback, and that a headless install is
# unaffected. Never touches the host: everything happens inside docker run.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH
mkdir -p reports/distro-logs

UBUNTU="ubuntu:26.04@sha256:da6fc2be547864451aa253836dd926da33623312df4a9a243e35dc877c378a78"

# image|package-manager install command for python3 + GTK4 + libadwaita + Xvfb + openssl
PLATFORMS=(
  "$UBUNTU|apt|ubuntu-26.04"
  "debian:trixie|apt|debian-trixie"
  "fedora:44|dnf|fedora-44"
  "almalinux:10|dnf|almalinux-10"
  "opensuse/tumbleweed:latest|zypper|opensuse-tumbleweed"
  "archlinux:latest|pacman|arch"
)

deps_for() {
  local manager=$1
  case "$manager" in
  apt)
    echo 'export DEBIAN_FRONTEND=noninteractive
# Quiet on success; on failure show the apt output, refresh the index and try once
# more. The Ubuntu archive is briefly inconsistent at times (an index naming a package
# version the pool answers 404 for), which the discarded output used to hide.
quiet_apt() {
  "$@" >/tmp/apt.log 2>&1 && return
  cat /tmp/apt.log >&2
  sleep 30
  apt-get update >/dev/null && "$@"
}
quiet_apt apt-get update
quiet_apt apt-get install -y --no-install-recommends python3 python3-gi python3-gi-cairo \
  gir1.2-gtk-4.0 gir1.2-adw-1 xvfb xauth librsvg2-common fonts-dejavu-core iproute2 openssl'
    ;;
  dnf)
    # RHEL 10 rebuilds ship no Xvfb and no broadway backend, so Xvfb is optional here;
    # the scenarios detect the missing display and skip only the widget checks.
    # libglvnd-gles: GTK 4.22's libepoxy aborts (SIGABRT, not a fallback) when
    # libGLESv2.so.2 cannot be dlopened; every desktop has it through its compositor,
    # a bare container does not, and the gtk4 package does not require it.
    echo 'dnf -y install python3 python3-gobject gtk4 libadwaita librsvg2 dejavu-sans-fonts iproute openssl >/dev/null 2>&1
dnf -y install xorg-x11-server-Xvfb xorg-x11-xauth libglvnd-gles >/dev/null 2>&1 || true'
    ;;
  zypper)
    echo 'zypper --non-interactive --gpg-auto-import-keys refresh >/dev/null 2>&1
zypper --non-interactive install python3 python3-gobject python3-gobject-Gdk \
  typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 xorg-x11-server-Xvfb xvfb-run xauth \
  gdk-pixbuf-loader-rsvg dejavu-fonts gawk iproute2 openssl >/dev/null 2>&1'
    ;;
  pacman)
    echo 'pacman -Sy --noconfirm >/dev/null 2>&1
pacman -S --noconfirm --needed python python-gobject gtk4 libadwaita xorg-server-xvfb \
  xorg-xauth librsvg ttf-dejavu iproute2 openssl >/dev/null 2>&1'
    ;;
  *)
    echo "no dependency step for package manager '$manager'" >&2
    return 1
    ;;
  esac
}

status=0
for entry in "${PLATFORMS[@]}"; do
  IFS='|' read -r image manager slug <<<"$entry"
  runner=$(mktemp)
  {
    echo 'set -euo pipefail'
    deps_for "$manager"
    cat <<'INNER'
mkdir -p /build && cp -a /src/. /build/ && cd /build
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" \
  || { echo "GTK4/libadwaita typelibs unavailable on this platform" >&2; exit 1; }
echo "  ok: GTK4 and libadwaita are available"

echo "== stage the payload =="
stage=$(mktemp -d)
WAYLAND_VNC_REPO_ROOT="$PWD" packaging/stage-payload.sh "$stage" /usr >/dev/null
test -x "$stage/usr/bin/wayland-vnc-settings" || { echo "settings launcher missing" >&2; exit 1; }
desktop="$stage/usr/share/applications/io.github.ventura8.wayland_vnc.Settings.desktop"
grep -q '^Exec=wayland-vnc-settings$' "$desktop" || { echo "desktop Exec wrong" >&2; exit 1; }
echo "  ok: launcher and desktop entry installed"

echo "== happy: the app is discoverable in the desktop app drawer =="
icon="$stage/usr/share/icons/hicolor/scalable/apps/io.github.ventura8.wayland_vnc.svg"
test -f "$icon" || { echo "hicolor icon missing" >&2; exit 1; }
python3 -c "import sys,xml.etree.ElementTree as E; E.parse(sys.argv[1])" "$icon" \
  || { echo "icon is not well-formed SVG" >&2; exit 1; }
echo "  ok: scalable hicolor icon installed and well-formed"
symbolic="$stage/usr/share/icons/hicolor/symbolic/apps/io.github.ventura8.wayland_vnc-symbolic.svg"
test -f "$symbolic" || { echo "symbolic icon missing" >&2; exit 1; }
python3 -c "import sys,xml.etree.ElementTree as E; E.parse(sys.argv[1])" "$symbolic" \
  || { echo "symbolic icon is not well-formed SVG" >&2; exit 1; }
# A symbolic icon is recoloured by the shell, so it must stay monochrome.
python3 - "$symbolic" <<'SYMBOLIC'
import re
import sys

source = open(sys.argv[1], encoding="utf-8").read()
colours = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{3,6}", source)}
assert len(colours) <= 1, f"symbolic icon is not monochrome: {sorted(colours)}"
assert 'viewBox="0 0 16 16"' in source, "symbolic icon is not on the 16x16 grid"
print("  ok: symbolic icon is monochrome on the 16x16 grid")
SYMBOLIC
# Well-formed XML is not enough: gdk-pixbuf sniffs the first bytes for the <svg
# signature, so anything pushing it out of that window (a long leading comment, for
# instance) makes the icon silently unloadable. Rasterise both to prove they render.
python3 - "$icon" "$symbolic" <<'RASTER'
import sys

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf

for path in sys.argv[1:]:
    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(path, 64, 64)
    assert pixbuf.get_width() > 0 and pixbuf.get_height() > 0, path
print("  ok: both icons actually rasterise through gdk-pixbuf")
RASTER
# The Icon key must name the installed icon (no extension), or drawers show a blank.
grep -q '^Icon=io.github.ventura8.wayland_vnc$' "$desktop" \
  || { echo "desktop Icon key does not match the installed icon" >&2; exit 1; }
echo "  ok: Icon key resolves to the installed icon"
# An entry that is hidden or lacks a type/name never reaches the drawer.
grep -q '^Type=Application$' "$desktop" || { echo "not a Type=Application entry" >&2; exit 1; }
grep -q '^Name=' "$desktop" || { echo "desktop entry has no Name" >&2; exit 1; }
grep -q '^Categories=' "$desktop" || { echo "desktop entry has no Categories" >&2; exit 1; }
if grep -qE '^(NoDisplay|Hidden)=true$' "$desktop"; then
  echo "desktop entry is hidden from the drawer" >&2
  exit 1
fi
echo "  ok: entry is visible, typed and categorised"
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$desktop" || { echo "desktop-file-validate failed" >&2; exit 1; }
  echo "  ok: desktop-file-validate passes"
fi
# Finally ask GIO to parse it the way a shell's app drawer does. GIO resolves the
# Exec binary, so the staged bin directory and icon theme must be visible first --
# exactly the state a real install leaves behind.
install -d "$HOME/.local/share/icons" 2>/dev/null || true
cp -a "$stage/usr/share/icons/hicolor" "$HOME/.local/share/icons/" 2>/dev/null || true
PATH="$stage/usr/bin:$PATH" XDG_DATA_HOME="$HOME/.local/share" \
  python3 - "$desktop" <<'DRAWER'
import sys
import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio

info = Gio.DesktopAppInfo.new_from_filename(sys.argv[1])
assert info is not None, "GIO could not parse the desktop entry"
assert info.should_show(), "the entry would not be shown in the app drawer"
assert info.get_icon() is not None, "the entry exposes no icon to the shell"
print(
    f"  ok: GIO shows '{info.get_display_name()}' with icon "
    f"'{info.get_icon().to_string()}'"
)
DRAWER

if command -v xvfb-run >/dev/null 2>&1; then
  echo "== happy: the installed launcher starts the window from the packaged tree =="
  set +e
  out=$(timeout 20 xvfb-run -a "$stage/usr/bin/wayland-vnc-settings" 2>&1)
  code=$?
  set -e
  case "$out" in
  *Traceback*) echo "launcher raised: $out" >&2; exit 1 ;;
  esac
  test "$code" -eq 124 || { echo "launcher exited early ($code): $out" >&2; exit 1; }
  echo "  ok: launcher runs (no traceback)"
  have_display=yes
else
  echo "== note: no headless display on this platform; widget scenarios will skip =="
  have_display=no
fi

echo "== scenarios (happy + bad) against the packaged app =="
work=$(mktemp -d)
scenarios=/build/scripts/settings_app_e2e_scenarios.py
if [ "$have_display" = yes ]; then
  xvfb-run -a python3 "$scenarios" "$stage/usr/lib/wayland-vnc" "$work"
else
  python3 "$scenarios" "$stage/usr/lib/wayland-vnc" "$work"
fi
INNER
  } >"$runner"

  echo "=== settings app e2e: $slug ==="
  if ! docker run --rm --network bridge \
    -v "$PWD:/src:ro" -v "$runner:/runner.sh:ro" \
    "$image" bash /runner.sh 2>&1 | tee "reports/distro-logs/settings-app-e2e-${slug}.log"; then
    status=1
    echo "settings app e2e failed on $slug" >&2
  fi
  rm -f "$runner"
done

without_gtk=$(mktemp)
cat >"$without_gtk" <<'INNER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# Quiet on success; on failure show apt's own output, refresh the index and try once
# more. Ubuntu's archive is briefly inconsistent at times (an index naming a package
# version the pool answers 404 for), which the discarded output used to hide.
quiet_apt() {
  "$@" >/tmp/apt.log 2>&1 && return
  cat /tmp/apt.log >&2
  sleep 30
  apt-get update >/dev/null && "$@"
}
quiet_apt apt-get update
quiet_apt apt-get install -y --no-install-recommends python3 openssl
mkdir -p /build && cp -a /src/. /build/ && cd /build
python3 -c "import gi" 2>/dev/null && { echo "GTK unexpectedly present" >&2; exit 1; }

echo "== bad: the settings app fails closed with an actionable hint =="
stage=$(mktemp -d)
WAYLAND_VNC_REPO_ROOT="$PWD" packaging/stage-payload.sh "$stage" /usr >/dev/null
set +e
out=$("$stage/usr/bin/wayland-vnc-settings" 2>&1)
code=$?
set -e
test "$code" -ne 0 || { echo "settings app should fail without GTK" >&2; exit 1; }
case "$out" in
*"needs GTK4 and libadwaita"*) echo "  ok: actionable install hint shown" ;;
*) echo "expected an install hint, got: $out" >&2; exit 1 ;;
esac
case "$out" in
*Traceback* | *ModuleNotFoundError*)
  echo "raw import traceback leaked to the user: $out" >&2
  exit 1
  ;;
esac
echo "  ok: no raw import traceback leaked"

echo "== happy: the CLI and service path are unaffected with no GTK installed =="
WAYLAND_VNC_CONFIG_DIR=$(mktemp -d)/cfg \
  WAYLAND_VNC_CLI="$stage/usr/bin/wayland-vnc" bash /build/scripts/package_smoke_scenarios.sh >/dev/null
echo "  ok: headless install is unaffected by the optional GUI"
echo "SETTINGS APP NO-GTK E2E PASSED"
INNER

echo "=== settings app e2e: no-gtk (ubuntu:26.04) ==="
docker run --rm --network bridge \
  -v "$PWD:/src:ro" -v "$without_gtk:/runner.sh:ro" \
  "$UBUNTU" bash /runner.sh 2>&1 | tee reports/distro-logs/settings-app-e2e-nogtk.log || status=1
rm -f "$without_gtk"

exit "$status"
