# wayland-vnc

[![Release asset downloads](https://img.shields.io/github/downloads/ventura8/wayland-vnc/total?label=release%20downloads&color=blue)](https://github.com/ventura8/wayland-vnc/releases)
[![Latest release](https://img.shields.io/github/v/release/ventura8/wayland-vnc?include_prereleases&label=latest&color=blue)](https://github.com/ventura8/wayland-vnc/releases)

Wayland-only VNC compatibility toolkit targeting **actual RealVNC Viewer** on Android
and desktop. Repository: `ventura8/wayland-vnc`.

The counter above is GitHub's own tally of release-asset downloads (the `.deb`,
`.rpm`, Arch, AppImage, Flatpak and Snap files plus `SHA256SUMS`). It cannot see
installs from a PPA or a distribution mirror, and it reads zero until a release with
assets is published.

**Status: packaged installer available; no desktop is release-qualified yet.** The
installed product is a real Wayland VNC server (WayVNC) plus a read-only diagnostic.
It listens on this machine only (`127.0.0.1:5900`) until you turn on **Local Network
Access** in the settings app, after which any device on any network this machine is
on can reach it -- a phone on the same Wi-Fi, and anything else on that Wi-Fi. There
is no fence beyond that switch: a systemd user service cannot filter traffic, so do
not forward the port from the Internet. A password is always required (a random one
is generated on first start; change it in the settings app). Per-desktop
compatibility is proved only by the actual-viewer qualification suite, which is not
yet complete for any target.

## Install

Ubuntu 26.04 can use the PPA, which every release tag populates (its changelog
states whether that release is qualified; none is yet):

```sh
sudo add-apt-repository ppa:ventura8/wayland-vnc
sudo apt update && sudo apt install wayland-vnc
# GNOME desktops: the patched GNOME Remote Desktop backend is in the PPA too.
sudo apt install wayland-vnc-grd
```

Everything else installs from the release assets (they also carry `wayland-vnc-grd`
for a GNOME desktop that does not use the PPA):

```sh
# A downloaded GitHub release asset (verify first). --ignore-missing checks the
# files you actually downloaded instead of failing on every other asset.
sha256sum -c --ignore-missing SHA256SUMS
sudo apt install ./wayland-vnc_*.deb          # Debian/Ubuntu
sudo dnf install ./wayland-vnc-*.noarch.rpm   # Fedora/EL

# GNOME desktops (Ubuntu 26.04, amd64 or arm64): the patched GNOME Remote Desktop
# backend. The distribution's own crashes whenever a viewer disconnects.
sudo apt install ./wayland-vnc-grd_*_"$(dpkg --print-architecture)".deb
```

Release assets ship for deb, rpm, Arch, AppImage, Flatpak, and Snap with a
`SHA256SUMS` manifest, plus `wayland-vnc-grd` for GNOME (the PPA builds it from
source as well). The Flatpak is the settings
window and the diagnostic only: its sandbox has no VNC backend, so it cannot serve;
to serve, install the deb, rpm or Arch package (or the AppImage or Snap).

## Launch the setup menu

`wayland-vnc-setup` presents a TUI with **install**, **diagnostics**, and (once
installed) **uninstall**. Or drive the CLI directly:

```sh
wayland-vnc doctor            # read-only capability diagnostic
wayland-vnc set-password      # store the viewer credential (mode 600)
systemctl --user enable --now wayland-vnc.service

# Development, from a source checkout:
PYTHONPATH=src python3 -m wayland_vnc doctor --json
```

GNOME targets a private GNOME Remote Desktop VNC build; KDE targets TigerVNC
`w0vncserver` through real portals; compatible capture/input protocols target WayVNC.
Desktop names alone are never accepted as proof of compatibility. Isolated
container fixtures exist for GNOME, KDE Plasma, Sway, labwc, Xfce+labwc, LXQt+labwc,
and Wayfire (`scripts/fixture-smoke.sh`). Disposable virtio-gpu KVM guests
(`scripts/kvm/build-guest.sh`, one per wlroots target; Hyprland only runs there)
add what a container cannot: a real ACPI S3 suspend/resume with the actual RealVNC
Viewer connected — partial evidence, not yet a full release qualification.

Stable release targets GNOME, Plasma, Xfce+labwc, LXQt+labwc, Sway, Hyprland, and
Wayfire on Ubuntu 26.04 amd64. Xfce is targeted only in the Xfce+labwc
configuration, where labwc is the compositor; Xfce's own Wayland session remains
upstream-experimental and is not a target. arm64 packages are built (the GNOME
backend deb, the AppImage, the snap and the flatpak) and the container smokes run
on arm64 in CI, but no arm64 hardware or viewer run exists, so arm64 is built and
smoke-tested, not qualified. Other distributions, compositors and architectures are
not implicitly supported.

See [development](docs/development.md), [security](SECURITY.md), and
[implementation status](docs/implementation-status.md). GPL-2.0-or-later; preserve
upstream dependency licenses. This project is not affiliated with RealVNC.
