# Implementation status

## Implemented

- Read-only, bounded capability probes and backend candidate selection.
- Versioned diagnostic JSON and typed GNOME, KWin-portal, and WayVNC backend contracts.
- Transactional staging installer with checksummed manifests, backup, rollback,
  symlink-escape defense, and local-change protection. Live-root setup remains disabled.
- Lifecycle start/stop commands fail closed without changing services.
- Project safety rules and initial human documentation.
- Actual RealVNC Viewer 7.15.1 completed an RA2/AES-256 connection to the
  isolated Sway/WayVNC fixture and captured correct, changing 1920x1080 RGBW
  frames. Keyboard text and a pointer click reached the native Wayland scene and
  produced independently detected cyan and magenta acknowledgement markers.
  This is partial desktop-viewer evidence only; reconnect stress, scaling,
  interruption, and Android remain unqualified.
- The same actual Viewer repeated colors, changing frames, keyboard, and pointer
  checks against an isolated labwc 0.9 fixture at 1920x1080 with XWayland
  disabled fail-closed. labwc is only the compositor foundation; the Xfce+labwc
  and LXQt+labwc targets need genuine desktop sessions and remain unqualified.
- Genuine Xfce 4.20 (`startxfce4 --wayland`) and LXQt 2.3 (`startlxqtwayland`)
  sessions on labwc, plus a headless Wayfire 0.10 fixture, each passed the same
  actual-Viewer colors, changing-frames, keyboard, and pointer checks at
  1920x1080 over RA2-256/AES-256. All of it is partial evidence; no target is
  qualified.
- Bounded in-container smoke checks for the Sway, labwc, Xfce+labwc, LXQt+labwc,
  and Wayfire fixtures verify compositor and session processes, sockets,
  listener, native scene, Wayland-only environment, no XWayland, and output
  mode, with guaranteed container cleanup.
- Isolated GNOME path: private patched GRD 50.2 VNC daemon linked to the private
  LibVNCServer, on real headless GNOME Shell 50.1 with a private system bus.
  Actual RealVNC Viewer passed colors, changing frames, keyboard and pointer at
  1920x1080 (VncAuth, loopback only). Partial evidence; the live service is untouched.
- Genuine KDE path: TigerVNC 1.16.2 `w0vncserver` (verified source build) through
  `xdg-desktop-portal-kde` RemoteDesktop/ScreenCast and PipeWire on real
  `kwin_wayland` + `plasmashell`, with the real consent dialog approved through
  AT-SPI. Actual RealVNC Viewer passed colors, changing frames, keyboard, pointer
  and portal-approve at 1920x1080. Needs a DRM render node; partial evidence only.
- Hyprland is blocked in containers: aquamarine needs a KMS DRM device or a
  dmabuf-capable parent compositor; a virtio-gpu KVM guest is required.

- Unattended actual-RealVNC scenario runner producing schema-v2 records:
  first frame, colors, changing frames, 1080p, twenty reconnects, viewer kill,
  network interruption, server restart, live resize and 4K/200% (wlroots), with
  post-run fixture health, the real portal approve/deny/restore decisions on
  Plasma, and unattended keyboard, pointer, scroll, drag, a real session lock
  and (on Sway) monitor hot-plug driven through an isolated viewer harness that
  hosts the actual RealVNC Viewer and injects input via WayVNC. Only
  `suspend-resume` (needs a VM) stays not-run, and Sway's `4k-200` scale-down
  and the occasional RealVNC RA2 reconnect handshake keep records `incomplete`,
  never falsely `passed`. Attended input evidence still merges through marker
  verification for runs without the harness.

## Required before prerelease

- Private backend builds, authenticated configuration, installation and rollback.
- Hardened native patches with sanitizer regressions and independently verified sources.
- Seven real Wayland desktop fixtures and actual RealVNC viewer automation.
- Full lint, coverage, packaging, and Android test tooling.
- Android app instrumentation and evidence records. Provisioning itself is done:
  the signature-verified official app (4.9.4.60176) has produced partial evidence
  against the KVM guest; see [android.md](android.md).

## Required before stable

All seven targets must pass both actual viewers, including rendered colors, changing
frames, input coordinates, twenty reconnects, network interruption, resize, portal
consent, lock and suspend recovery. Record exact versions, commit and renderer.
Nightly stress runs require one hundred reconnects. No missing test may become a
passing qualification record. A successful prototype connection is not a release.
