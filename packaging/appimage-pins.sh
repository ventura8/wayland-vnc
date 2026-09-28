# shellcheck shell=bash
# The AppImage toolchain, pinned once for every build that makes an AppImage: the
# release build (scripts/build_release_package.sh) and the portable smoke
# (scripts/run_portable_package_smoke.sh). Sourced, never executed.
#
# appimagetool embeds a runtime (the ELF stub that mounts the squashfs) into every
# AppImage. Left to itself it downloads that runtime from type2-runtime's mutable
# `continuous` release with no checksum, so pinning only the tool still shipped
# whatever `continuous` held on the day. Both are tagged releases, verified by
# SHA-256; the runtime digests were checked against the release's own GPG signatures
# (key 570C 77AC EA40 C0F1 B758 902C BF96 CCA5 6490 F695) when they were pinned.

appimagetool_version=1.9.1
appimage_runtime_version=20251108

# appimage_pins ARCH: set appimagetool_sha256 and appimage_runtime_sha256 for ARCH
# (x86_64 or aarch64); fails for any other architecture.
appimage_pins() {
  case "$1" in
  x86_64)
    appimagetool_sha256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
    appimage_runtime_sha256=2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d
    ;;
  aarch64)
    appimagetool_sha256=f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158
    appimage_runtime_sha256=00cbdfcf917cc6c0ff6d3347d59e0ca1f7f45a6df1a428a0d6d8a78664d87444
    ;;
  *)
    echo "no pinned AppImage toolchain for architecture '$1'" >&2
    return 1
    ;;
  esac
}

# appimage_fetch URL SHA256 DEST: download URL over HTTPS only (every redirect too),
# bounded in time, and install it at DEST only if it matches SHA256. A cached DEST is
# re-verified instead of trusted: it is executed or embedded with the privileges of
# the build, so a tampered or half-written copy must never be used.
appimage_fetch() {
  local url=$1 sha256=$2 dest=$3
  if [[ ! -f "$dest" ]]; then
    curl -fsSL --proto =https --proto-redir =https --connect-timeout 30 --max-time 600 \
      -o "$dest.download" "$url"
    mv -- "$dest.download" "$dest"
  fi
  if ! printf '%s  %s\n' "$sha256" "$dest" | sha256sum -c - >/dev/null 2>&1; then
    rm -f -- "$dest" "$dest.download"
    echo "$url failed its checksum; refusing to use it" >&2
    return 1
  fi
}

# appimage_toolchain ARCH DIR: fetch and verify appimagetool and the runtime for ARCH
# into DIR, as DIR/appimagetool-ARCH (made executable) and DIR/runtime-ARCH.
appimage_toolchain() {
  local arch=$1 dir=$2
  appimage_pins "$arch" || return 1
  local tool="$dir/appimagetool-$arch" runtime="$dir/runtime-$arch"
  appimage_fetch \
    "https://github.com/AppImage/appimagetool/releases/download/${appimagetool_version}/appimagetool-${arch}.AppImage" \
    "$appimagetool_sha256" "$tool" || return 1
  appimage_fetch \
    "https://github.com/AppImage/type2-runtime/releases/download/${appimage_runtime_version}/runtime-${arch}" \
    "$appimage_runtime_sha256" "$runtime" || return 1
  chmod +x -- "$tool"
}
