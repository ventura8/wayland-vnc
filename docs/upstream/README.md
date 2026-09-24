# Upstream reports

Seven defects in this project's upstream dependencies, each found with the actual
RealVNC Viewer (Android or desktop) against a real desktop -- this laptop's GNOME, or a
KVM guest in the qualification lab -- and each reduced to a reproduction that needs
neither this project nor a phone. Every file here is a complete issue body, ready to paste; nothing in it refers
to wayland-vnc except as the reporter.

LibVNCServer:

- [libvncserver-01-depth.md](libvncserver-01-depth.md) — `rfbGetScreen` announces
  depth 32 for an 8/3/4 screen while the ZRLE encoder sends 3-byte CPIXELs.
- [libvncserver-02-message-drain.md](libvncserver-02-message-drain.md) — an update is
  encoded in the old pixel format although `SetPixelFormat` has already arrived.

Hyprland's aquamarine (found on a QEMU virtio-gpu output, reduced to plain Hyprland in a
guest):

- [aquamarine-01-modeset-with-stale-framebuffer.md](aquamarine-01-modeset-with-stale-framebuffer.md)
  — a live switch to any larger mode is committed with the previous framebuffer still
  attached; virtio-gpu rejects it and the output falls back to 800x600.

GNOME Remote Desktop:

- [gnome-remote-desktop-01-disconnect-crash.md](gnome-remote-desktop-01-disconnect-crash.md)
  — NULL dereference in `grd-session-vnc.c` when a client disconnects inside
  `rfbProcessEvents`.
- [gnome-remote-desktop-02-queued-dead-connections.md](gnome-remote-desktop-02-queued-dead-connections.md)
  — connections whose peer hung up stay queued and block the single VNC session.
- [gnome-remote-desktop-03-realtime-data-loop-killed.md](gnome-remote-desktop-03-realtime-data-loop-killed.md)
  — the daemon is SIGKILLed mid-stream when the realtime PipeWire data loop copies
  4K MemFd frames for 200 ms without sleeping (`RLIMIT_RTTIME`).
- [gnome-remote-desktop-04-mirrored-monitor-resize-freezes.md](gnome-remote-desktop-04-mirrored-monitor-resize-freezes.md)
  — a mirror-primary session keeps a stream Mutter has ended after the monitor
  changed size, and the viewer freezes on its last frame.

## Where each one goes

- LibVNCServer: <https://github.com/LibVNC/libvncserver/issues/new>. Both bodies can be
  filed from a terminal, for example:

  ```bash
  gh issue create --repo LibVNC/libvncserver \
    --title "rfbGetScreen announces depth 32 for an 8/3/4 screen, but ZRLE sends 3-byte CPIXELs" \
    --body-file docs/upstream/libvncserver-01-depth.md
  ```

- GNOME Remote Desktop:
  <https://gitlab.gnome.org/GNOME/gnome-remote-desktop/-/issues/new>. A GNOME GitLab
  account is required; paste the body and keep the title from the file's first heading.

The patches referenced by the reports are in [`patches/`](../../patches), are derived
from the upstream sources they apply to, and carry the upstream licence
(GPL-2.0-or-later). They apply to the pristine release tarballs with `--fuzz=0`, so
they can be offered as merge or pull requests unchanged.

## Reproduction

`scripts/reproduce-upstream-libvncserver.sh` builds pristine LibVNCServer 0.9.15 from
its published tarball, verifies the digest, runs upstream's own
`examples/server/example`, and probes it. `--with-fix` applies the two candidate fixes
and shows that neither defect survives. Everything runs inside `docker run --rm`.
