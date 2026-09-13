# rfbGetScreen announces depth 32 for an 8/3/4 screen, but ZRLE sends 3-byte CPIXELs

## Summary

`rfbGetScreen(argc, argv, w, h, bitsPerSample=8, samplesPerPixel=3, bytesPerPixel=4)`
sets `screen->depth = 8 * bytesPerPixel`, so ServerInit announces **depth 32** even
though the caller described 24 bits of colour and the red, green and blue maxima sent
in the same message are 255 each.

The ZRLE encoder does not use that depth. For 32 bits per pixel it picks the 3-byte
CPIXEL form whenever the colour fits in three bytes
(`src/libvncserver/zrle.c`, `fitsInLS3Bytes`). A client that keeps the server's pixel
format therefore computes the CPIXEL size from the announced depth of 32, reads 4-byte
pixels, and decodes garbage a few hundred milliseconds into the session.

RealVNC Viewer for Android is such a client: it reports `bad xrle data`, drops the
stream and reconnects, forever. Viewers that negotiate depth 24 with `SetPixelFormat`
before the first update never see it.

## Reproduction

Upstream 0.9.15 and upstream's own example server; no patched code involved.

```sh
cmake -S . -B out -DWITH_EXAMPLES=ON && cmake --build out
./out/examples/server/example -rfbport 5999
```

Connect with any client that does **not** send `SetPixelFormat`, asks for ZRLE
(encoding 16) and requests one update. ServerInit reports:

```text
bits-per-pixel 32, depth 32, maxima (255, 255, 255)
```

and the ZRLE tile stream that follows is self-consistent only when parsed as 3-byte
CPIXELs. A client honouring depth 32 parses 4-byte CPIXELs and runs off the end of the
first tile row.

A scripted version of exactly this, which downloads the 0.9.15 tarball, verifies its
digest, builds it and prints both facts, is here:
<https://github.com/ventura8/wayland-vnc/blob/main/scripts/reproduce-upstream-libvncserver.sh>

## Candidate fix

Announce the depth the caller actually described. In `src/libvncserver/main.c`:

```diff
-   screen->bitsPerPixel = screen->depth = 8*bytesPerPixel;
+   screen->bitsPerPixel = 8*bytesPerPixel;
+   screen->depth = bitsPerSample*samplesPerPixel;
```

With that change the same probe reports `depth 24` and the 3-byte CPIXELs the encoder
was already sending become correct. Verified with the script above (`--with-fix`).

The other direction would be to make the ZRLE encoder honour the announced depth
rather than only the colour maxima, which keeps ServerInit unchanged but sends four
bytes per pixel where three were enough. The one-line change above looks like the
smaller and more truthful fix, but the choice is yours.

## Downstream

GNOME Remote Desktop's VNC backend calls `rfbGetScreen(..., 8, 3, 4)`
(`src/grd-session-vnc.c`), so every GNOME desktop served over VNC announces depth 32
and is unusable from viewers that keep the server format. My build of it works around
this by overriding `screen->depth` after the call, which is a downstream patch I would
rather drop.

## Environment

LibVNCServer 0.9.15 (release tarball, sha256
`62352c7795e231dfce044beb96156065a05a05c974e5de9e023d688d8ff675d7`), built with GCC on
Ubuntu 26.04, x86-64. Observed with RealVNC Viewer 7.15.1 for Android and with a
minimal RFB 3.8 client.
