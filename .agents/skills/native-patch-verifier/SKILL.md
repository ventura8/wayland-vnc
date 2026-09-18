---
name: native-patch-verifier
description: >-
  Verify the native LibVNCServer/GNOME Remote Desktop/TigerVNC patch series and generated
  systemd units under sanitizers, from checksum-pinned upstream sources.
---

# Native Patch Verifier Skill

Use this skill to validate the project's native patch series and the generated
systemd user units before they ship in a fixture image or a package.

## Source integrity

- Every upstream archive is pinned by SHA-256 in `sources.json`
  (gnome-remote-desktop, libvncserver, tigervnc). `scripts/prepare-source.py NAME
  ARCHIVE` verifies the digest, extracts into an isolated `artifacts/sources/` tree,
  and applies the ordered patch series with **zero fuzz** (`patch --fuzz=0`).
- Keep correctness fixes separate from empirical compatibility workarounds, and
  preserve upstream licensing. Patches carry an upstream/license note in their header.
- The `docker/Dockerfile.gnome` and `docker/Dockerfile.plasma` build stages fetch the
  same pinned archives, verify the digest with `sha256sum -c`, apply the patch series
  fuzz-free, and link the daemon to the private library through an RPATH.

## Sanitizer regression

```bash
set -euo pipefail
mkdir -p reports/native
bash scripts/test-native.sh 2>&1 | tee reports/native/asan.log
```

`scripts/test-native.sh` builds the buffer-layout regression with
`-fsanitize=address,undefined -fno-omit-frame-pointer -Werror -Wconversion` and runs
it with `ASAN_OPTIONS=detect_leaks=1`. Exercise cursor disconnect, partial
file-descriptor duplication failure, invalid buffer metadata, and encoding
renegotiation under the sanitizers. Passing helper tests alone does not establish
daemon integration or viewer correctness — that is the `fixture-qualifier` skill.

## Systemd unit round trip

```bash
set -euo pipefail
mkdir -p reports/native
bash scripts/test-units.sh 2>&1 | tee reports/native/units.log
```

`scripts/test-units.sh` generates each backend's user unit, verifies it with
`systemd-analyze --root ... verify`, uninstalls it, and fails if any staged file
remains. Generated units carry `NoNewPrivileges` and, deliberately, neither a mount
sandbox nor an `IPAddressAllow`: in a user manager the mount directives only create an
AppArmor-restricted user namespace that breaks `serve` on GNOME, and the fence is not
applied at all. The server binds loopback by default and the local network only when
the user turns that switch on.

## Rules

- No unconditional encoding override: the private LibVNCServer patch prefers advertised
  ZRLE, otherwise Raw, on every SetEncodings message, and never retains a withdrawn
  encoding. That policy lives only in the project's private library, never the system
  library.
- Never patch the host's live troubleshooting source tree. Build only against the
  checksum-verified isolated copy.
