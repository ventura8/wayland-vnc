# An update is encoded in the old pixel format although SetPixelFormat has arrived

## Summary

`rfbCheckFds()` processes **one** client message per pass, and `rfbProcessEvents()`
flushes a pending framebuffer update between passes. When a client sends several
messages in one packet, the server can therefore encode and send an update in the old
pixel format while the client's `SetPixelFormat` is already sitting in the socket
buffer, unread.

RFB has no per-update format tag, so such a client cannot decode what it gets. RealVNC
Viewer for Android sends `SetPixelFormat` immediately after its probe format and
reports `bad xrle data`, drops the connection and retries; on a busy desktop it took
between one and six attempts before one happened to land between frames.

The window is only open when the server sends immediately, which is
`deferUpdateTime == 0`. GNOME Remote Desktop's VNC backend sets exactly that
(`rfb_screen->deferUpdateTime = 0` in `src/grd-session-vnc.c`), and so will any
latency-sensitive server. With the default 5 ms deferral the remaining messages are
usually read before the flush, which is why this is rarely seen.

## Reproduction

Upstream 0.9.15 and upstream's own example server, in immediate-send mode:

```sh
./out/examples/server/example -rfbport 5999 -deferupdate 0
```

Then, as a client:

1. `SetPixelFormat` to an 8-bit format, ask for ZRLE.
2. Dirty the screen (a pointer event with a button held draws), request an update,
   consume it.
3. `FramebufferUpdateRequest(incremental=1)` while the screen is clean, so the request
   stays pending with nothing to send.
4. In **one** `write()`: pointer events that dirty the screen, followed by
   `SetPixelFormat` to a 32-bit format.

The next updates arrive encoded 8-bit, although the server has already been handed the
32-bit format. In a run against 0.9.15 the first five updates after the change were
still in the old format:

```text
pixel sizes of the updates after SetPixelFormat(32-bit): [1], [1], [1], [1], [1], [3], [3], ...
```

Scripted, digest-verified reproduction:
<https://github.com/ventura8/wayland-vnc/blob/main/scripts/reproduce-upstream-libvncserver.sh>

## Candidate fix

Drain what has already arrived before returning to the send path, which is what the
WebSockets build already does for its own buffer:

```diff
+                    int drained = 0;
 #ifdef LIBVNCSERVER_WITH_WEBSOCKETS
                     do {
                         rfbProcessClientMessage(cl);
-                    } while (cl->sock != RFB_INVALID_SOCKET && webSocketsHasDataInBuffer(cl));
+                    } while (cl->sock != RFB_INVALID_SOCKET &&
+                             (webSocketsHasDataInBuffer(cl) ||
+                              (++drained < rfbMaxDrainedMessages && rfbClientInputPending(cl))));
 #else
-                    rfbProcessClientMessage(cl);
+                    do {
+                        rfbProcessClientMessage(cl);
+                    } while (cl->sock != RFB_INVALID_SOCKET &&
+                             ++drained < rfbMaxDrainedMessages &&
+                             rfbClientInputPending(cl));
 #endif
```

where `rfbClientInputPending()` is a `poll(POLLIN, timeout 0)` on the client socket.
The budget (64 messages) keeps a client that always has data from holding the event
loop; anything left over is read on the next iteration, by which time the update it
was competing with has gone out. The budget deliberately applies only to the socket
drain this adds: payload already decoded into the WebSockets buffer keeps draining
exactly as it does today, because no further socket activity will wake the loop for
bytes that have already been read off it.

The full patch, which applies to 0.9.15 with `--fuzz=0`, is attached below. Verified
with the script above (`--with-fix`): the same sequence then answers in the new format
every time, over eight consecutive runs.

`handleEventsEagerly` is not a substitute: it re-runs the whole select loop for every
client, whereas this only keeps reading the client whose data is already there.

I am aware that RFC 6143 §7.5.1 does not define when the new format takes effect, and
that clients needing certainty use the fence extension. This is a robustness fix: a
message the server has already been given should be applied before the next update is
encoded.

## Patch

`patches/libvncserver/0002-drain-client-messages.patch` in
<https://github.com/ventura8/wayland-vnc> (GPL-2.0-or-later, derived from the file it
patches). Happy to open it as a pull request if you prefer.

## Environment

LibVNCServer 0.9.15 (release tarball, sha256
`62352c7795e231dfce044beb96156065a05a05c974e5de9e023d688d8ff675d7`), GCC, Ubuntu
26.04, x86-64. Field case: GNOME Remote Desktop 50.2 on Ubuntu 26.04 with RealVNC
Viewer 7.15.1 for Android.
