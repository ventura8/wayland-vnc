# wayland-vnc RPM spec. Version is read from the repo-root VERSION at build time
# (rpmbuild --define "vnc_version $(cat VERSION)"); no tracked semver to bump here.
%global debug_package %{nil}
%global __python %{__python3}

Name:           wayland-vnc
Version:        %{?vnc_version}%{!?vnc_version:0.0.0}
Release:        1%{?dist}
Summary:        Wayland VNC server and compatibility toolkit for RealVNC viewers

License:        GPL-2.0-or-later
URL:            https://github.com/ventura8/wayland-vnc
BuildArch:      noarch

Requires:       python3
Requires:       wayvnc
Requires:       openssl
# The GTK settings app is optional: recommended so desktops get it automatically,
# not required so headless server installs stay lean.
Recommends:     python3-gobject
# The settings window uses Adw.Dialog (libadwaita 1.5) and Adw.SpinRow/ToolbarView
# (1.4); libadwaita 1.5 itself needs GTK 4.14. Unversioned, the package would install
# against an older runtime and the window would fail on a missing widget.
Recommends:     gtk4 >= 4.14
Recommends:     libadwaita >= 1.5
# The app icon is an SVG; without a loader the desktop shows no artwork.
Recommends:     librsvg2

%description
wayland-vnc makes a native Wayland desktop reachable with actual RealVNC viewers.
It installs a systemd user service that runs the WayVNC server inside the
logged-in Wayland session, provisioning helpers, and a read-only capability
diagnostic (wayland-vnc doctor). Installing enables and starts the service for every
logged-in user, generating a random viewer password on first start so the server is
never unauthenticated. It binds 127.0.0.1:5900, this machine only, until Local
Network Access is turned on in the settings app; the server then binds every
interface and is reachable from every network this machine is on (a user service
cannot fence that, so do not forward the port from the internet). It installs no
system service and never modifies a live remote-desktop session.

%prep
# Sources are provided directly in the build tree by the smoke/release driver.

%build

%install
rm -rf %{buildroot}
WAYLAND_VNC_REPO_ROOT="%{_sourcedir}/repo" \
  "%{_sourcedir}/repo/packaging/stage-payload.sh" "%{buildroot}" /usr

%files
%{_bindir}/wayland-vnc
%{_bindir}/wayland-vnc-setup
%{_bindir}/wayland-vnc-settings
%{_datadir}/applications/io.github.ventura8.wayland_vnc.Settings.desktop
%{_datadir}/icons/hicolor/scalable/apps/io.github.ventura8.wayland_vnc.svg
%{_datadir}/icons/hicolor/symbolic/apps/io.github.ventura8.wayland_vnc-symbolic.svg
%dir %{_prefix}/lib/wayland-vnc
%dir %{_prefix}/lib/wayland-vnc/wayland_vnc
%{_prefix}/lib/wayland-vnc/wayland_vnc/*.py
%{_prefix}/lib/systemd/user/wayland-vnc.service
%dir %{_datadir}/wayland-vnc
%{_datadir}/wayland-vnc/schema-v2.json
%{_datadir}/wayland-vnc/VERSION
# Translation catalogues. rpmbuild fails the build on installed-but-unpackaged
# files, so every language staged into share/locale must be listed here; the
# per-language directories themselves belong to the filesystem package.
%{_datadir}/locale/*/LC_MESSAGES/wayland-vnc.mo
%doc %{_datadir}/doc/wayland-vnc/*

%post
if command -v systemctl >/dev/null 2>&1; then
    systemctl --global enable wayland-vnc.service >/dev/null 2>&1 || :
    # Activate now for users already logged in, by entering each one's own session
    # manager where its XDG_RUNTIME_DIR and D-Bus socket live. The unit fails closed
    # without a stored credential, so only start where one exists; everyone else
    # gets it at their next login from the --global enable above.
    for uid in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $1}'); do
        user=$(id -nu "$uid" 2>/dev/null) || continue
        runtime="/run/user/$uid"
        [ -S "$runtime/bus" ] || continue
        # No credential yet is fine: serve generates a random 600 one on first run.
        runuser -u "$user" -- env XDG_RUNTIME_DIR="$runtime" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus" \
            systemctl --user enable --now wayland-vnc.service >/dev/null 2>&1 || :
    done
fi

# Refresh the desktop and icon caches so the settings app appears in the app drawer.
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q %{_datadir}/applications || :
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -qtf %{_datadir}/icons/hicolor || :
fi

%preun
if [ "$1" = 0 ] && command -v systemctl >/dev/null 2>&1; then
    # Stop the service for every logged-in user, not just future logins: removing
    # the package must not leave a VNC server running.
    for uid in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $1}'); do
        user=$(id -nu "$uid" 2>/dev/null) || continue
        runtime="/run/user/$uid"
        [ -S "$runtime/bus" ] || continue
        runuser -u "$user" -- env XDG_RUNTIME_DIR="$runtime" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus" \
            systemctl --user disable --now wayland-vnc.service >/dev/null 2>&1 || :
    done
    systemctl --global disable wayland-vnc.service >/dev/null 2>&1 || :
fi

%postun
# The payload lives outside site-packages, so a runtime .pyc tree would be left
# unowned by %files. Remove exactly that and nothing else: `rm -rf` on the whole
# directory would also delete anything another package or an administrator put
# there, and rpm already removes every file this package owns.
if [ "$1" = 0 ] && [ -d %{_prefix}/lib/wayland-vnc ]; then
    # Exactly the directory our own modules would byte-compile into. A recursive
    # find over the whole tree would also delete caches belonging to anything an
    # administrator or another package placed underneath it.
    rm -rf -- %{_prefix}/lib/wayland-vnc/wayland_vnc/__pycache__ 2>/dev/null || :
    # Drop the now-empty directories we own; rmdir refuses if anything remains.
    rmdir %{_prefix}/lib/wayland-vnc/wayland_vnc %{_prefix}/lib/wayland-vnc \
        2>/dev/null || :
fi

# Refresh the desktop and icon caches so the settings app appears in the app drawer.
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q %{_datadir}/applications || :
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -qtf %{_datadir}/icons/hicolor || :
fi

%changelog
* Mon Sep 14 2026 ventura8 <alexandrescu.sergiu@gmail.com> - 1.0.0-1
- Initial wayland-vnc packaging: WayVNC user service, provisioning, diagnostics.
