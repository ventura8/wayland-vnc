# VNC: a mirror-primary session freezes on its last frame when the mirrored monitor changes size

## Summary

A VNC session in the default mirror-primary mode records the primary monitor with
`RecordMonitor`. When that monitor changes size -- a new mode or scale in Settings --
Mutter ends the monitor's screen-cast stream: its logical monitor no longer matches the
one the stream was created for.

```c
/* mutter 50.1, src/backends/meta-screen-cast-monitor-stream.c */
static gboolean
update_monitor (MetaScreenCastMonitorStream *monitor_stream,
                MetaMonitor                 *new_monitor)
{
  ...
  if (!mtk_rectangle_equal (&new_logical_monitor->rect,
                            &monitor_stream->logical_monitor->rect))
    return FALSE;
  ...
}

static void
on_monitors_changed (MetaMonitorManager          *monitor_manager,
                     MetaScreenCastMonitorStream *monitor_stream)
{
  ...
  if (!new_monitor || !update_monitor (monitor_stream, new_monitor))
    meta_screen_cast_stream_close (META_SCREEN_CAST_STREAM (monitor_stream));
}
```

For a remote desktop session Mutter then only drops the stream (`on_stream_closed` in
`meta-screen-cast-session.c` closes the session for a plain screen cast, but not for
`META_SCREEN_CAST_SESSION_TYPE_REMOTE_DESKTOP`). The `org.gnome.Mutter.ScreenCast.Stream`
interface has no signal for this, so nothing reaches the daemon over D-Bus: the PipeWire
node simply disappears, and the daemon's `pw_stream` falls back from `streaming` to
`paused`.

The VNC session does not notice either. `GrdVncPipeWireStream` emits `closed` only on a
core `EPIPE` (`on_core_error`), so `on_pipewire_stream_closed` in `grd-session-vnc.c`
never runs; the session keeps the dead stream, the framebuffer keeps its old size, and
the client stays connected to a picture that never changes again. The resize handling
the session already has (`grd_session_vnc_queue_resize_framebuffer`, NewFBSize) is never
reached, because no stream ever reports the new size.

## Reproduction

A GNOME session (Mutter 50.1 here) with the VNC backend enabled in screen-share mode,
and any VNC viewer that supports DesktopSize (RealVNC Viewer, TigerVNC's vncviewer):

1. Connect; the viewer shows the desktop at, say, 1920x1080.
2. In Settings, switch the display to 1280x720 (or change its scale), and keep the
   change.
3. The viewer keeps showing the last 1920x1080 frame. The desktop no longer updates,
   input still arrives, and the session only ends when the viewer disconnects.

With `G_MESSAGES_DEBUG=all` the daemon logs, at the moment of the switch, only:

```text
gnome-remote-desktop-daemon: Pipewire stream state changed from streaming to paused
```

and nothing after it -- no `Stream parameters changed`, no `PipeWire stream closed`.
`pw-dump` in the session shows the monitor's video node gone.

## Fix

Two parts, both in the VNC backend:

- `GrdVncPipeWireStream` gets the registry from its core and, in `global_remove`,
  emits a new `source-removed` signal when the removed global is its source node. It is
  kept apart from `closed`, which still means the PipeWire connection itself failed and
  still closes the client.
- In mirror-primary mode, the session answers `source-removed` by recording the
  primary monitor again (from an idle: the stream objects cannot be freed inside their
  own callback); a virtual-monitor session closes the client. The old `GrdStream` is
  dropped without calling `Stop`, since Mutter has already removed it;
  `grd_session_record_monitor` starts the new one, whose format negotiation calls
  `grd_session_vnc_queue_resize_framebuffer` with the new size, and the client receives
  a DesktopSize update.

A patch against 50.2 that does exactly this, about 110 lines:
`patches/gnome-remote-desktop/0009-follow-mirrored-monitor-resize.patch` in the
reporter's repository. With it, the same switch to 1280x720 continues the session at the
new size (daemon log: `[VNC] Source node 51 removed`, `Mirrored monitor stream ended,
recording it again`, `Stream parameters changed. New monitor size: [1280, 720]`), and
RealVNC Viewer for Android reports a 1280x720 desktop.

A cleaner long-term fix could live in Mutter: resizing the monitor stream in place, or
signalling the stream's end on D-Bus, would let every client handle this without
watching PipeWire. Either way the VNC backend should not keep a session on a stream
that no longer exists.

## Environment

- gnome-remote-desktop 50.2 (the defect is unchanged on `main` as of 2026-09-14),
  VNC backend, mirror-primary
- Mutter / GNOME Shell 50.1, Ubuntu 26.04, a QEMU/KVM guest on virtio-gpu
- RealVNC Viewer for Android 4.9.4
- Found by the wayland-vnc qualification suite's live-resize scenario; reported here
  without anything of that project in the reproduction.
