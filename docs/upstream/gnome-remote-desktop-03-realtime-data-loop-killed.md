# VNC: the daemon is SIGKILLed mid-stream when the PipeWire data loop copies MemFd frames for 200 ms without sleeping

## Summary

For a MemFd (system-memory) stream, the VNC PipeWire stream copies every frame on
PipeWire's data loop thread:

```c
/* src/grd-vnc-pipewire-stream.c */
 826 process_frame_data (GrdVncPipeWireStream *stream,
 852   if (buffer->datas[0].type == SPA_DATA_MemFd)
 860       map = mmap (NULL, size, PROT_READ, MAP_PRIVATE, buffer->datas[0].fd, 0);
 869       copy_frame_data (frame, src_data, width, height, dst_stride, src_stride, bpp);
 873       munmap (map, size);
 985 on_stream_process (void *user_data)   /* the pw_stream process callback */
```

That thread is not an ordinary one. libpipewire's client configuration loads
`module-rt`, which asks rtkit to schedule the data loop `SCHED_RR` and, because rtkit
requires it, lowers the process's `RLIMIT_RTTIME` to rtkit's ceiling (200 000 µs on
Ubuntu; `rtkit-daemon --rttime-usec-max`). The kernel's realtime watchdog sends
`SIGKILL` to a realtime thread that consumes that much CPU without sleeping once
(`setrlimit(2)`, `RLIMIT_RTTIME`: the count resets only when the thread blocks).

At 3840x2160 the copy is 33 MB per frame. When the compositor delivers frames faster
than the copies complete, `epoll_wait` finds the next event already pending, the loop
never blocks, and the budget is exhausted: the whole daemon dies with `SIGKILL`, the
session with it, and systemd restarts a daemon that will do it again. It is not
deterministic — it needs the machine to be busy enough that copies keep overrunning
the frame interval — so it presents as "the connection drops now and then on a 4K
screen".

Observed on the thread itself during a session, sampling `/proc/<pid>/task/<tid>/`:

```text
data-loop.0 policy=2 (SCHED_RR) RLIMIT_RTTIME=200000
total CPU 4677 ms in 6.75 s of session, 92 sleeps
longest run without sleeping: 239 ms CPU over 250 ms wall  → SIGKILL
```

and in the journal, each time:

```text
rtkit-daemon[...]: Successfully made thread <tid> of process <pid> owned by '1000' RT at priority 20.
gnome-remote-desktop.service: Main process exited, code=killed, status=9/KILL
```

Five sessions out of six died within ten seconds of streaming.

Builds that negotiate DMA-BUF are not affected on this path: the download runs on
the EGL thread. The MemFd path is taken whenever there is no EGL thread (no render
node, a virtual machine) or DMA-BUF was refused.

## Reproduction

1. GNOME Remote Desktop 50.2, `-Dvnc=true`, on a session whose PipeWire stream is
   MemFd (no EGL thread; the simplest is a virtual machine without a render node, or
   a build with the DMA-BUF buffer type left out of `allowed_buffer_types`).
2. A 3840x2160 desktop with something animating (a terminal scrolling is enough).
3. Any VNC viewer that keeps asking for updates: RealVNC Viewer 7.8.0 for Windows on a
   gigabit LAN was used here, at ZRLE, with the viewer's own line-speed estimate
   around 78 Mbit/s.
4. Watch the thread: once `rtkit-daemon` reports the promotion, read
   `/proc/<pid>/task/*/limits` for the thread named `data-loop.0` — `Max realtime
   timeout 200000` — and poll its `voluntary_ctxt_switches` and `schedstat`; the
   daemon is killed the first time the thread runs 200 ms of CPU without a voluntary
   switch.

No wayland-vnc component is involved; the daemon was started by its systemd user
unit and the client is an unmodified commercial viewer.

## Fix

Either keep the heavy work off the realtime thread (hand the MemFd buffer to the
main context, as the DMA-BUF path already hands it to the EGL thread), or tell
libpipewire that this client does not want a realtime data loop. The second is a
one-liner and is what wayland-vnc carries, because a screen-capture consumer gains
nothing from realtime scheduling that is worth a `SIGKILL`:

```c
  stream->pipewire_context =
    pw_context_new (pipewire_source->pipewire_loop,
                    pw_properties_new ("module.rt", "false", NULL),
                    0);
```

`module.rt = false` is the condition under which `client.conf` skips `module-rt`
(PipeWire 1.0 and later: `condition = [ { module.rt = !false } ]`). With it the
thread stays `SCHED_OTHER`, no `RLIMIT_RTTIME` is set, and a frame the loop is late
for is dropped by PipeWire as before. Verified with the same viewer and screen: eight
sessions of eight completed, each containing 275–522 ms stretches of CPU without a
sleep that would have been a kill under the previous scheduling. Older PipeWire
ignores the property.

The patch, which applies to the pristine 50.2 tarball
(sha256 `31df628f4113573f136ffc8dc001763aed633cd85bed21e58bc61f8a17df091f`) with
`--fuzz=0`: `patches/gnome-remote-desktop/0008-pipewire-data-loop-not-realtime.patch`
in <https://github.com/ventura8/wayland-vnc>. GPL-2.0-or-later, derived from the file
it patches; happy to open it as a merge request, or to rework it as the "copy on the
main context" variant if that is preferred.

## Environment

Ubuntu 26.04, x86-64, kernel 7.0 (`CONFIG_HZ=1000`), PipeWire 1.6.2, rtkit with the
default 200 ms `rttime-usec-max`, GNOME Remote Desktop 50.2 built from the upstream
tarball with `-Dvnc=true`, LibVNCServer 0.9.15, a 3840x2160 laptop panel. Client:
RealVNC Viewer 7.8.0 for Windows on Windows 11, wired gigabit LAN.
