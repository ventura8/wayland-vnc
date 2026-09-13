# Native candidate builds

These commands build in isolation; **do not run `meson install` against the host**.
The original working service must remain untouched. Source hashes are in
`sources.json`; the GNOME hash was also checked against its official checksum file.

**Why the pinned versions are the latest ones we can use.** LibVNCServer 0.9.15 and
TigerVNC 1.16.2 are the newest upstream releases. GNOME Remote Desktop stays on
**50.2**, the last of the 50 series, on purpose: it builds against the Mutter of its
own series, and the target platform (Ubuntu 26.04) ships GNOME 50. A 51 series exists
upstream, but moving to it would mean building against a Mutter this desktop does not
have, would invalidate the patch series in `patches/gnome-remote-desktop`, and
would no longer match the version the upstream defect reports in `docs/upstream/`
cite. Revisit when the target distribution moves to GNOME 51.

Prepare downloaded upstream archives with:

```sh
python3 scripts/prepare-source.py gnome-remote-desktop /path/to/archive.tar.xz
python3 scripts/prepare-source.py libvncserver /path/to/archive.tar.gz
```

The helper verifies hashes, extracts into a new artifact directory, then applies
patches with zero fuzz. Use the printed source directories below.

```sh
cmake -S "$libvnc_source" -B artifacts/libvnc-build \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DBUILD_SHARED_LIBS=ON \
  -DWITH_EXAMPLES=OFF -DWITH_TESTS=OFF -DCMAKE_BUILD_TYPE=Debug
cmake --build artifacts/libvnc-build -j 4
scripts/test-encoding.sh "$libvnc_source" artifacts/libvnc-build
meson setup artifacts/grd-build "$grd_source" \
  -Dvnc=true -Drdp=false -Dman=false -Dtests=true -Dsystemd=false \
  --prefix=/opt/wayland-vnc/grd
meson compile -C artifacts/grd-build -j 4
```

`docker/Dockerfile.gnome` performs the same two builds inside an isolated image
(archives fetched and digest-checked, patches applied with zero fuzz) and links
the daemon to the private library through an RPATH; that is the build the GNOME
fixture evidence comes from.

The LibVNC build above disables upstream tests only for this dedicated parser test;
it is not a complete native qualification build. The GNOME build currently links
the distribution library unless explicitly packaged with the private LibVNC build.
Private linkage/RPATH and install isolation remain packaging acceptance gates.

## Patch policy

GNOME patches guard disconnected clients, retain single-axis cursor movement,
initialize optional descriptors, clean up failed duplication, and validate MemFd
layout before copying. Layout helper tests use ASan/UBSan. Full daemon integration,
including descriptor ownership and malicious buffer lifecycle, remains unqualified.

The private LibVNC compatibility patch chooses advertised ZRLE, otherwise Raw, on
every SetEncodings message. It never forces an unadvertised encoding or retains ZRLE
after withdrawal. The actual parser is exercised over a private socket pair.
This policy belongs only in the project's private library, not the system library.

The fourth GNOME patch supplies the separate MemFd-only and clipboard-off profile.
Controlled A/B experiments can set `WAYLAND_VNC_ENABLE_DMABUF=1` or
`WAYLAND_VNC_ENABLE_CLIPBOARD=1`. These are experimental lab overrides, not claims
that either subsystem was the original black-screen cause.
