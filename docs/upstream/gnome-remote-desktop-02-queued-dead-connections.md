# VNC: connections whose peer hung up stay queued and block the single session

## Summary

The VNC server allows one session at a time
(`grd_throttler_limits_set_max_global_connections (limits, 1)` in
`src/grd-vnc-server.c`) and the throttler queues further connections per peer. That
queue is pruned only with `g_io_stream_is_closed()`:

```c
/* src/grd-throttler.c */
228 prune_closed_connections (GQueue *queue)
238       if (g_io_stream_is_closed (G_IO_STREAM (connection)))
```

`g_io_stream_is_closed()` is true only for streams **we** closed. A client that gives
up while waiting — a viewer's connect timeout, a phone retrying — leaves a connection
whose peer is gone but which we never closed, so it stays in the queue. When the live
session ends, `dispatch_delayed_connections()` accepts that dead connection, a
`GrdSessionVnc` is created on a socket with nobody at the other end, and because only
one session is allowed, every real client behind it is delayed until that session
dies on its own.

In practice: a viewer that loops on reconnect, and eventually cannot connect at all.

## Reproduction

No special client needed; three plain TCP connections to port 5900 are enough.

1. **A** connects and completes a session, holding it.
2. **B** connects while A is live, so it is queued, waits about a second, and closes.
3. **A** closes.
4. **C** connects.

Expected: C gets the RFB version string immediately. Observed: C gets nothing, because
the queue handed the slot to B's corpse. Measured on Ubuntu 26.04 with GNOME Remote
Desktop 50.2: C received no version string within 40 seconds, and the daemon's own
debug log showed `[Throttler] Delaying connection` for it.

## Fix

Ask the socket instead of asking ourselves. First the kernel's hang-up flags:
`POLLRDHUP` is the peer's FIN even while bytes it sent before leaving are still
unread (a peek alone would see those bytes and call the connection alive),
`POLLHUP`/`POLLERR` a reset. Then a zero-length `MSG_PEEK | MSG_DONTWAIT` read is the
FIN with nothing queued, and an error other than "nothing yet" is a reset. Either way
the entry is dead and should be closed and dropped before dispatching:

```c
static gboolean
connection_peer_gone (GSocketConnection *connection)
{
  GSocket *socket;
  struct pollfd pfd;
  char byte;
  ssize_t n;

  if (g_io_stream_is_closed (G_IO_STREAM (connection)))
    return TRUE;

  socket = g_socket_connection_get_socket (connection);
  if (!socket)
    return TRUE;

  pfd.fd = g_socket_get_fd (socket);
  pfd.events = POLLIN | POLLRDHUP;
  pfd.revents = 0;
  if (poll (&pfd, 1, 0) == 1 && (pfd.revents & (POLLRDHUP | POLLHUP | POLLERR)))
    return TRUE;

  n = recv (g_socket_get_fd (socket), &byte, 1, MSG_PEEK | MSG_DONTWAIT);
  if (n == 0)
    return TRUE;
  if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)
    return TRUE;

  return FALSE;
}
```

used by `prune_closed_connections()` in place of the bare
`g_io_stream_is_closed()` check (`POLLRDHUP` is behind `_GNU_SOURCE` in glibc; the
patch defines it as the kernel's 0x2000 where the build does not set that macro). With it, the same three-step reproduction gives C the
version string in 0.11 s.

The patch, which applies to the pristine 50.2 tarball
(sha256 `31df628f4113573f136ffc8dc001763aed633cd85bed21e58bc61f8a17df091f`) with
`--fuzz=0`: `patches/gnome-remote-desktop/0006-queued-dead-connections.patch` in
<https://github.com/ventura8/wayland-vnc>. GPL-2.0-or-later, derived from the file it
patches; happy to open it as a merge request.

Peeking a byte does not consume it, so a connection that is merely idle is left
untouched and is still handed to `GrdSessionVnc` with its data intact.

## Environment

Ubuntu 26.04, x86-64, GNOME Remote Desktop 50.2 built from the upstream tarball with
`-Dvnc=true`, LibVNCServer 0.9.15. Seen in the field with RealVNC Viewer 7.15.1 for
Android on the same LAN.
