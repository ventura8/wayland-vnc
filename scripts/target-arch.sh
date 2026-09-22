#!/usr/bin/env bash
# The one place architecture is spelled out. Everything this repository builds or
# smokes that is architecture-specific (the GNOME backend package, the AppImage
# runtime, the container platform) asks here instead of assuming x86.
#
#   target-arch.sh deb        -> amd64 | arm64          (Debian spelling)
#   target-arch.sh platform   -> linux/amd64 | linux/arm64  (Docker spelling)
#   target-arch.sh appimage   -> x86_64 | aarch64       (AppImage / uname spelling)
#
# The default is the host, so an arm64 machine or CI runner builds arm64 without
# being told. WAYLAND_VNC_ARCH=amd64|arm64 overrides it for an emulated run on
# another host; DOCKER_DEFAULT_PLATFORM is honoured too, so one variable can drive
# both this script and every plain `docker run` in the smokes.
set -euo pipefail

spelling=${1:?"usage: target-arch.sh deb|platform|appimage"}

host_arch() {
  case "$(uname -m)" in
  x86_64) echo amd64 ;;
  aarch64 | arm64) echo arm64 ;;
  *)
    echo "target-arch.sh: unsupported host architecture $(uname -m)" >&2
    exit 2
    ;;
  esac
}

arch=${WAYLAND_VNC_ARCH:-}
if [[ -z $arch && -n ${DOCKER_DEFAULT_PLATFORM:-} ]]; then
  arch=${DOCKER_DEFAULT_PLATFORM#linux/}
fi
if [[ -z "$arch" ]]; then
  arch=$(host_arch)
fi
case "$arch" in
amd64 | arm64) ;;
*)
  echo "target-arch.sh: unsupported architecture '$arch' (amd64 or arm64)" >&2
  exit 2
  ;;
esac

case "$spelling" in
deb) echo "$arch" ;;
platform) echo "linux/$arch" ;;
appimage) [[ "$arch" = amd64 ]] && echo x86_64 || echo aarch64 ;;
*)
  echo "target-arch.sh: unknown spelling '$spelling' (deb, platform or appimage)" >&2
  exit 2
  ;;
esac
