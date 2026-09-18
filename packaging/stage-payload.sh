#!/usr/bin/env bash
# Stage the wayland-vnc payload into a DESTDIR shared by every package kind
# (deb, rpm, arch, appimage, flatpak, snap). The payload is the serving runtime and
# its CLI, the GTK settings application, the hardened systemd *user* unit, the
# desktop entry and icons, and the translation catalogues. Callers pass the package
# root (e.g. debian/wayland-vnc or an rpm buildroot); paths below are relative to it.
#
# Staging only lays files down. Enabling and starting the unit is the packages' own
# post-install step, and no system service is ever installed: the unit is per user.
set -euo pipefail

destdir=${1:?"usage: stage-payload.sh DESTDIR [PREFIX]"}
prefix=${2:-/usr}
repo_root=${WAYLAND_VNC_REPO_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}

site="$destdir$prefix/lib/wayland-vnc/wayland_vnc"
bindir="$destdir$prefix/bin"
sharedir="$destdir$prefix/share/wayland-vnc"
docdir="$destdir$prefix/share/doc/wayland-vnc"
unitdir="$destdir$prefix/lib/systemd/user"

install -d -m 755 "$site" "$bindir" "$sharedir" "$docdir" "$unitdir"

# The installed product imports only these runtime modules; qualification/fixture
# tooling (image_evidence, fixture_smoke, desktop_scenarios, manual_input,
# qualification, __main__) is not shipped, so the package has no Pillow dependency.
for module in __init__ cli runtime probe backends installer tui settings settings_app \
  settings_dialogs i18n; do
  install -m 644 "$repo_root/src/wayland_vnc/$module.py" "$site/$module.py"
done

# Console launcher: a thin wrapper so the packaged CLI needs no site-packages entry.
cat >"$bindir/wayland-vnc" <<'LAUNCH'
#!/usr/bin/env python3
import os
import sys

sys.dont_write_bytecode = True  # keep the packaged tree free of untracked .pyc
# Resolve the module tree relative to this launcher so the same payload works from
# any prefix: /usr (deb/rpm/arch), $SNAP/usr (snap), /app (flatpak), or an AppDir.
_here = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(_here, os.pardir, "lib", "wayland-vnc"))
from wayland_vnc.cli import main

raise SystemExit(main())
LAUNCH
chmod 755 "$bindir/wayland-vnc"

# Launcher TUI: `wayland-vnc-setup` presents install / diagnostics / uninstall.
cat >"$bindir/wayland-vnc-setup" <<'MENU'
#!/usr/bin/env python3
import os
import sys

sys.dont_write_bytecode = True
_here = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(_here, os.pardir, "lib", "wayland-vnc"))
from wayland_vnc.tui import main

raise SystemExit(main())
MENU
chmod 755 "$bindir/wayland-vnc-setup"

# Settings app: a native GTK4/libadwaita window over the same runtime.
cat >"$bindir/wayland-vnc-settings" <<'SETTINGS'
#!/usr/bin/env python3
import os
import sys

sys.dont_write_bytecode = True
_here = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(_here, os.pardir, "lib", "wayland-vnc"))
from wayland_vnc.settings_app import main

raise SystemExit(main(sys.argv[1:]))
SETTINGS
chmod 755 "$bindir/wayland-vnc-settings"

# Desktop entry so the settings app appears as a normal application.
install -d -m 755 "$destdir$prefix/share/applications"
cat >"$destdir$prefix/share/applications/io.github.ventura8.wayland_vnc.Settings.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Wayland VNC
Comment=Configure the Wayland VNC server and view its status
Exec=wayland-vnc-settings
Icon=io.github.ventura8.wayland_vnc
Categories=Network;RemoteAccess;
Terminal=false
DESKTOP
chmod 644 "$destdir$prefix/share/applications/io.github.ventura8.wayland_vnc.Settings.desktop"

# Scalable hicolor icon so the entry shows with artwork in every desktop's app drawer.
icondir="$destdir$prefix/share/icons/hicolor/scalable/apps"
install -d -m 755 "$icondir"
install -m 644 "$repo_root/packaging/icons/io.github.ventura8.wayland_vnc.svg" \
  "$icondir/io.github.ventura8.wayland_vnc.svg"
# Symbolic variant: GNOME uses it wherever the app is shown at small sizes.
symdir="$destdir$prefix/share/icons/hicolor/symbolic/apps"
install -d -m 755 "$symdir"
install -m 644 "$repo_root/packaging/icons/io.github.ventura8.wayland_vnc-symbolic.svg" \
  "$symdir/io.github.ventura8.wayland_vnc-symbolic.svg"

# Compiled translation catalogues. The settings window resolves these relative to its
# own location, so the same payload works from /usr, $SNAP/usr, /app or an AppDir.
"$repo_root/scripts/build-translations.sh" compile >/dev/null
for mo in "$repo_root"/build/locale/*/LC_MESSAGES/wayland-vnc.mo; do
  [ -e "$mo" ] || continue
  lang=$(basename "$(dirname "$(dirname "$mo")")")
  install -d -m 755 "$destdir$prefix/share/locale/$lang/LC_MESSAGES"
  install -m 644 "$mo" "$destdir$prefix/share/locale/$lang/LC_MESSAGES/wayland-vnc.mo"
done

# Public qualification schema and the version stamp the CLI can report.
install -m 644 "$repo_root/qualification/schema-v2.json" "$sharedir/schema-v2.json"
install -m 644 "$repo_root/VERSION" "$sharedir/VERSION"

# Hardened systemd user unit: the installed server runs inside the user's Wayland
# session and is enabled per user (never a system unit). It listens on the local
# network, with the unit's IPAddressAllow fencing traffic to loopback and
# private/link-local ranges so port 5900 never reaches the internet.
install -m 644 "$repo_root/packaging/systemd/wayland-vnc.service" \
  "$unitdir/wayland-vnc.service"

# Documentation shipped with every package kind.
install -m 644 "$repo_root/README.md" "$docdir/README.md"
install -m 644 "$repo_root/SECURITY.md" "$docdir/SECURITY.md"
for doc in development testing implementation-status; do
  [ -f "$repo_root/docs/$doc.md" ] && install -m 644 "$repo_root/docs/$doc.md" "$docdir/$doc.md"
done

echo "staged wayland-vnc payload into $destdir$prefix"
