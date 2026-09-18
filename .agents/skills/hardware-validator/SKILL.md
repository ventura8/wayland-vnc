---
name: hardware-validator
description: >-
  Validate the packaged wayland-vnc on real hardware end to end -- install, app
  drawer, service activation, the actual RealVNC Viewer, uninstall -- and record an
  honest phase report. Use before a release, after touching packaging, the systemd
  unit, the serving path, or the settings app.
---

# Hardware Validator Skill

Runs the built package on a real desktop and proves the product works there, using
the **actual RealVNC Viewer** rather than a mock. Containers cannot cover this: the
installer's user-service activation, the app drawer, the icon theme and a real network
interface only exist on a live session.

## When to use it

- Before cutting a release, on each desktop being claimed.
- After changing `debian/`, `packaging/`, the systemd unit, `runtime.serve`, or the
  settings app.
- When a user reports "installed but not running" -- this reproduces the whole path.

## Prerequisites

The script refuses rather than guesses if any are missing:

- a live **Wayland** session (not a container -- it checks `/.dockerenv`)
- passwordless `sudo` for `apt`
- the real **RealVNC Viewer** (`vncviewer`; the banner is checked for "RealVNC")
- `Xvfb`, to host the viewer without disturbing the desktop
- a built package: run `scripts/run_deb_package_smoke.sh` first, or set
  `WAYLAND_VNC_DEB=/path/to.deb`

## Running it

```sh
# The session bus must be present: grdctl and the keyring are reached over D-Bus.
XDG_RUNTIME_DIR=/run/user/$(id -u) \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus" \
  ./scripts/run_hardware_validation.sh
```

It leaves the machine **installed and running** (there is a final reinstall phase), and
writes `artifacts/hardware/<stamp>/report.json` plus viewer logs and captured frames.

## Phases

| Phase | Proves |
| --- | --- |
| `install` | the real postinst enables **and starts** the unit, with no manual step |
| `drawer` | the desktop entry is in the app drawer and both icons resolve from `/usr/share/icons/hicolor` |
| `service` | port 5900 is served; records which backend won (`wayvnc` or `grd`) |
| `password` | a viewer password can be set through the product itself |
| `e2e-loopback` | the actual RealVNC Viewer authenticates on `127.0.0.1:5900` and a real frame of this desktop arrives |
| `e2e-lan` | the same over the LAN address the settings app shows -- what a phone uses |
| `bad-password` | the real server refuses a wrong password |
| `uninstall` | purge stops and disables our unit, leaves nothing behind, and does **not** stop the desktop's own remote-desktop daemon |
| `reinstall` | the machine is left working |

## Reading the result

Each phase is `passed`, `failed`, or `blocked`.

- **blocked** means a prerequisite could not be met on that host. It is recorded with
  the reason and does **not** fail the run -- but it is never a pass. Do not claim a
  desktop is validated on the strength of blocked phases.
- **failed** fails the run immediately and writes the report before exiting.

`result` in `report.json` is `passed` only when **every** phase passed. Any blocked
phase makes the whole run `incomplete`, and any failed phase makes it `failed`. A
run that is `incomplete` has not validated this desktop; say so rather than
reporting it as a pass.

## Known blocked path: the grd backend

On GNOME, `serve` delegates to GNOME Remote Desktop. Its VNC password lives in the
login keyring, and on Ubuntu's `gnome-remote-desktop 50.2+vnc` a password stored via
`grdctl vnc set-password` is not accepted by the running server for a standard VncAuth
handshake -- verified by setting a known password, restarting the daemon, and offering
both the raw string and the GVariant-quoted form the keyring actually holds; both were
refused. The viewer phases are therefore `blocked` on a grd host.

Prove the viewer path on a **wayvnc** backend instead, where the credential is our own
file: a wlroots desktop (Sway, Hyprland, labwc), or the KVM Hyprland guest
(`scripts/kvm/build-hyprland-guest.sh`), which has passed with this exact viewer.

## The helpers are unit-tested

The fiddly parts live in `src/wayland_vnc/hardware.py`, covered by
`tests/test_hardware.py`, so they are not shell-only:

- `obfuscate_password` -- RealVNC's stored-password DES obfuscation, pinned against a
  known-good `vncpasswd` vector. If it drifts, every generated `.vnc` silently stops
  working.
- `connection_file` -- a private `.vnc` whose plaintext password never appears.
- `frame_evidence` -- fails closed unless a capture is a real desktop, so a black or
  "connecting" screen cannot be mistaken for success.
- `Report` -- the phase record; `blocked` never counts as a pass, and a run holding
  one reports `incomplete` rather than `passed`.
- `grd_stored_password` -- reads grd's effective password from the keyring.

## Safety

- It changes the machine deliberately: installs, purges and reinstalls the package,
  and sets a viewer password. It prints where that password is written.
- On a GNOME host it will have changed the desktop's **GNOME Remote Desktop VNC
  password**, because that is where `set-password` syncs. Tell the user.
- It never touches the desktop's own remote-desktop daemon beyond that, and asserts
  the daemon is still running after purge.
