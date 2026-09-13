# VNC: NULL dereference when a client disconnects inside rfbProcessEvents

## Summary

`handle_socket_data()` in `src/grd-session-vnc.c` calls `rfbProcessEvents()` and then
reads `session_vnc->rfb_client->preferredEncoding`. Processing those events can
disconnect the client synchronously, and `handle_client_gone()` clears
`session_vnc->rfb_client`, so the read dereferences NULL and the daemon dies with
SIGSEGV. In 50.2 that is:

```c
/* src/grd-session-vnc.c */
720          rfbProcessEvents (session_vnc->rfb_screen, 0);
721
722          if (session_vnc->pending_framebuffer_resize &&
723              session_vnc->rfb_client->preferredEncoding != -1)
```

Every disconnect can hit it: a viewer closing the window, a wrong password, a network
timeout. systemd restarts the daemon, the viewer reconnects, and with a client that
retries automatically this becomes an endless loop in which no session ever survives.

## Evidence

Ubuntu 26.04, `gnome-remote-desktop 50.2-0ubuntu0.1+vnc3+vnc44` (50.2 plus Ubuntu's
VNC-enabling patches), reproduced on every single disconnect, dozens of times:

```text
kernel: gnome-remote-de[270283]: segfault at 50 ip 0000621acb3a11b3 sp 00007ffe49352210 \
        error 4 in gnome-remote-desktop-daemon[901b3,621acb328000+80000]
systemd[6703]: gnome-remote-desktop.service: Main process exited, code=dumped, status=11/SEGV
```

The faulting address is `0x50` and `offsetof(rfbClientRec, preferredEncoding)` is
`0x50` (LibVNCServer 0.9.15, x86-64), so the faulting object is a NULL
`session_vnc->rfb_client`. Disassembling the daemon at the faulting offset shows the
load is the instruction right after the call to `rfbProcessEvents@plt`:

```text
90188:  call   1bcb0 <rfbIsActive@plt>
901a6:  call   1c0b0 <rfbProcessEvents@plt>
901b3:  mov    0x50(%rax),%ecx        <-- faults
901b6:  cmp    $0xffffffff,%ecx       <-- preferredEncoding != -1
```

## Fix

Return as soon as the client is gone, the same guard the surrounding code already uses
elsewhere:

```diff
           rfbProcessEvents (session_vnc->rfb_screen, 0);
 
+          /* Processing input may synchronously disconnect the client. */
+          if (!session_vnc->rfb_client)
+            return G_SOURCE_REMOVE;
+
           if (session_vnc->pending_framebuffer_resize &&
               session_vnc->rfb_client->preferredEncoding != -1)
```

A second place needs the same care: `maybe_queue_cursor_move()` reads
`session_vnc->rfb_screen->cursorX` for cursor metadata that can arrive after
`clientGone` has cleared `rfb_client`.

Built from the pristine 50.2 tarball
(sha256 `31df628f4113573f136ffc8dc001763aed633cd85bed21e58bc61f8a17df091f`) with this
guard added, the daemon survives every disconnect: two consecutive 15-second sessions
closed by the client, thirty update requests each, zero restarts, where the
distribution build crashed on every close.

The patch, which applies to the 50.2 tarball with `--fuzz=0`:
`patches/gnome-remote-desktop/0001-client-lifetime.patch` in
<https://github.com/ventura8/wayland-vnc>. GPL-2.0-or-later, derived from the file it
patches; I am happy to open it as a merge request.

## Caveat

The crashes above were observed on Ubuntu's build, which enables VNC through
distribution patches. The faulting code path is upstream code and the guard is
against upstream sources, but I have not run an unpatched upstream build with
`-Dvnc=true` side by side. If that distinction matters for triage, tell me and I will
produce it.

## Environment

Ubuntu 26.04, x86-64, GNOME Shell 50, Mutter headless and a physical session,
LibVNCServer 0.9.15. Clients: RealVNC Viewer 7.15.1 (desktop and Android) and a
minimal RFB 3.8 client that only completes VncAuth and closes.
