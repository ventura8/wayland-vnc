#!/usr/bin/env bash
# Build and boot a disposable KVM guest for one qualification target, for the
# scenarios no container can provide: ACPI S3 suspend-resume, and on the desktops
# whose compositors need a real DRM device, output modes, hot-plug and the session
# lock. The guest is the container fixture's own session script, scene and smoke
# contract on a virtio-gpu KMS device (Mesa llvmpipe EGL/GBM) with a seat, provisioned
# by cloud-init from tests/kvm/wlroots.user-data.template (Hyprland, which no
# container can start at all, is one of these targets). Nothing touches the host
# GPU or the live desktop; the guest is a throwaway qcow2 overlay and the repo is
# shared read-only over 9p for the first boot only (the guest copies what it needs
# and unmounts it: virtio-9p does not survive S3). The VNC port is reachable only on
# host loopback via user-net hostfwd -- or, with --harness, on the host's address on
# the internal Docker lab network, where the viewer harness container can reach it and
# nothing outside the lab can. Credentials come from the private fixture.conf, the
# server identity from the private fixture-rsa.pem (never argv).
#
#   scripts/kvm/build-guest.sh <target> [--harness]
#   targets: hyprland sway wayfire xfce-labwc lxqt-labwc gnome plasma
#
# The wlroots-style targets use tests/kvm/wlroots.user-data.template; GNOME and Plasma
# use tests/kvm/desktop.user-data.template (a logind session, the desktop's own lock
# screen). GNOME's guest installs the project's private daemon from
# artifacts/grd-deb/wayland-vnc-grd_*_amd64.deb (scripts/build_grd_package.sh); Plasma's
# takes the project's TigerVNC w0vncserver out of the wayland-vnc-plasma:dev image
# into artifacts/kvm/tigervnc (done here).
#
# Each target gets its own work directory (artifacts/kvm/<target>) and forwarded port,
# so several guests can run side by side on one host.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
umask 077

target=${1:-}
case "$target" in
hyprland | sway | wayfire | xfce-labwc | lxqt-labwc) template=tests/kvm/wlroots.user-data.template ;;
gnome | plasma) template=tests/kvm/desktop.user-data.template ;;
*)
  echo "usage: $0 <hyprland|sway|wayfire|xfce-labwc|lxqt-labwc|gnome|plasma> [--harness]" >&2
  exit 2
  ;;
esac
shift
harness=0
if [[ "${1:-}" = "--harness" ]]; then
  harness=1
  shift
fi
[[ $# -eq 0 ]] || {
  echo "unexpected arguments: $*" >&2
  exit 2
}

# One port per target, so guests can coexist; WAYLAND_VNC_KVM_PORT overrides.
case "$target" in
hyprland) default_port=5920 ;;
sway) default_port=5921 ;;
wayfire) default_port=5922 ;;
xfce-labwc) default_port=5923 ;;
lxqt-labwc) default_port=5924 ;;
gnome) default_port=5925 ;;
plasma) default_port=5926 ;;
*)
  echo "no port assigned for target '$target'" >&2
  exit 2
  ;;
esac
port=${WAYLAND_VNC_KVM_PORT:-$default_port}
work="artifacts/kvm/$target"
mkdir -p "$work"
# The virtio-gpu device. The wlroots guests take a 4K EDID and set custom modes; the
# full-desktop guests (GNOME/Plasma) go through the compositor's DisplayConfig, which
# offers only advertised modes, so they turn EDID off -- the virtio-gpu driver then
# exposes its full common-mode list (720p and 2160p included), which DisplayConfig
# needs for the resize and 4K scenarios.
case "$target" in
# The desktop guests carry a custom EDID (720p + 1080p + 4K) their compositor's
# DisplayConfig needs, loaded over the connector in the guest, and a second output
# for the monitor-change scenario.
# Behind a PCIe root port with No_Soft_Reset set, the GPU keeps its state across S3:
# on the root bus QEMU resets it on resume, the compositor's buffers vanish, and
# KWin's page flips time out forever after ("Pageflip timed out!"), a black desktop.
gnome | plasma)
  gpu=(
    -device "pcie-root-port,id=gpu-port,bus=pcie.0,chassis=1"
    -device "virtio-gpu-pci,bus=gpu-port,x-pcie-pm-no-soft-reset=on,max_outputs=2,edid=on,max_hostmem=1G"
  )
  ;;
*) gpu=(-device "virtio-gpu-pci,max_outputs=1,xres=3840,yres=2160,max_hostmem=1G") ;;
esac
# max_hostmem caps the total size of the guest's 2D framebuffer resources, 256 MiB by
# default: eight 3840x2160 buffers. Hyprland keeps more than that alive through a mode
# change -- the old 1080p swapchain, the new 4K one, WayVNC's capture pool and the
# cursor -- and the device refused RESOURCE_CREATE_2D with OUT_OF_MEMORY (0x1201),
# the output fell back to 800x600 and WayVNC crashed. The other compositors fitted
# under the default by chance, not by design.
# The guest's VNC port is forwarded to host loopback in every mode. With --harness the
# viewer runs in a container on the host network namespace, which reaches 127.0.0.1;
# for the Android viewer adb reverse points the emulator at the same loopback port.
# Loopback keeps the guest reachable only from the host, never the LAN.
bind=${WAYLAND_VNC_KVM_BIND:-127.0.0.1}
[[ "$harness" -eq 1 ]] && echo "== harness mode: guest VNC on $bind (host-network viewer) =="
image_url="https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img"
# `current/` is a mutable path Ubuntu republishes, so the digest is pinned and checked
# below. This one was taken from that day's SHA256SUMS after verifying its detached
# signature against Ubuntu's UEC image signing key, fingerprint
# D2EB 4462 6FDD C30B 513D  5BB7 1A5D 6C4C 7DB8 7C81 -- never from the image alone,
# which would only prove the download matched itself:
#   curl -fsSLO .../SHA256SUMS -O .../SHA256SUMS.gpg
#   gpg --verify SHA256SUMS.gpg SHA256SUMS && grep amd64.img SHA256SUMS
image_sha256=2d3b9b1f76fc204f684a2313113b1d7c2b35eabba19cfcbcec5eae2aed3cc853
base="artifacts/kvm/resolute-cloudimg-amd64.img"
overlay="$work/guest.qcow2"
seed="$work/seed.iso"
vars="$work/OVMF_VARS.fd"
credential=${WAYLAND_VNC_CREDENTIAL:-artifacts/desktop-viewer/fixture.conf}
server_key=${WAYLAND_VNC_SERVER_KEY:-artifacts/desktop-viewer/fixture-rsa.pem}

[[ -f "$credential" ]] || {
  echo "Missing private credential file: $credential" >&2
  exit 2
}
# Everything after the FIRST "=" is the password: it may contain "=" itself.
password=$(sed -n 's/^password=//p' "$credential" | head -1)
[[ -n "$password" ]] || {
  echo "No password in $credential" >&2
  exit 2
}
[[ -f "$server_key" ]] || {
  echo "Missing private server key: $server_key (the harness pins this identity)" >&2
  exit 2
}
case "$target" in
gnome)
  ls artifacts/grd-deb/wayland-vnc-grd_*_amd64.deb >/dev/null 2>&1 || {
    echo "GNOME's guest installs the private daemon from artifacts/grd-deb/wayland-vnc-grd_*_amd64.deb;" >&2
    echo "build it first: scripts/build_grd_package.sh" >&2
    exit 2
  }
  ;;
plasma)
  # The project's TigerVNC w0vncserver, taken out of the Plasma fixture image once
  # (a copy brought from another lab machine is kept as it is).
  if [[ ! -x artifacts/kvm/tigervnc/bin/w0vncserver ]]; then
    echo "== TigerVNC w0vncserver from the Plasma fixture image =="
    docker image inspect wayland-vnc-plasma:dev >/dev/null 2>&1 ||
      docker build -q -f docker/Dockerfile.plasma -t wayland-vnc-plasma:dev . >/dev/null
    rm -rf artifacts/kvm/tigervnc
    staging=$(docker create wayland-vnc-plasma:dev)
    docker cp "$staging:/opt/wayland-vnc/tigervnc" artifacts/kvm/tigervnc >/dev/null
    docker rm -f "$staging" >/dev/null
  fi
  ;;
*) ;; # the wlroots guests install everything from the archive
esac

echo "== base cloud image =="
# Verified against a pinned digest. `current/` is a mutable path that Ubuntu replaces
# whenever it republishes the image, so without this two runs could use different
# guest contents while both claiming to be the same evidence environment. Refresh
# image_sha256 deliberately when moving to a newer image, and re-record the evidence.
if [[ ! -f "$base" ]]; then
  curl -fSL -o "$base.part" "$image_url"
  if ! printf '%s  %s\n' "$image_sha256" "$base.part" | sha256sum -c - >/dev/null 2>&1; then
    echo "Cloud image does not match the pinned digest $image_sha256." >&2
    echo "Ubuntu has republished $image_url; verify the new image and update" >&2
    echo "image_sha256 in this script before trusting evidence built on it." >&2
    rm -f -- "$base.part"
    exit 2
  fi
  mv "$base.part" "$base"
fi

# A guest from an earlier run may still be up: recreating its overlay underneath it
# and starting a second QEMU on the same forwarded port would break both. Refuse
# while its pid is alive; otherwise clear what it left (the pid file and the two
# control sockets), so this QEMU binds fresh ones.
if [[ -f "$work/qemu.pid" ]] && kill -0 "$(cat "$work/qemu.pid")" 2>/dev/null; then
  echo "a $target guest is already running (pid $(cat "$work/qemu.pid")); stop it first:" >&2
  echo "  kill \$(cat $work/qemu.pid)" >&2
  exit 2
fi
rm -f "$work/qemu.pid" "$work/qga.sock" "$work/qmp.sock"

echo "== disposable overlay =="
rm -f "$overlay"
qemu-img create -f qcow2 -F qcow2 -b "$(realpath "$base")" "$overlay" 20G >/dev/null

echo "== cloud-init seed ($target) =="
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
hash=$(printf '%s' "$password" | openssl passwd -6 -stdin)
# The desktop guests' custom EDID, base64 for the template's write_files block.
edid_b64=""
case "$target" in
gnome | plasma)
  python3 scripts/kvm/make-edid.py "$tmp/edid.bin"
  edid_b64=$(base64 -w0 "$tmp/edid.bin")
  ;;
*) ;; # the wlroots guests set their own modes and need no EDID
esac
export WAYLAND_VNC_TEMPLATE_EDID_B64="$edid_b64"
# Substituted literally, and through the environment rather than argv. sed would
# interpret "&" and "\\" in the replacement, and bash's own ${var//pat/repl} expands
# an unescaped "&" to the matched text (bash 5.2), so both would corrupt a password
# that contains those characters. In the template the password sits in a literal
# block scalar (write_files), where every character is taken as is; the only thing
# that could break that is a line break, which a one-line credential cannot carry.
# The package list is the target's container image's own (docker/Dockerfile.<target>),
# so the guest runs the same distribution packages the container fixture qualified
# with; the server key is indented into its block scalar line by line.
WAYLAND_VNC_TEMPLATE_HASH="$hash" WAYLAND_VNC_TEMPLATE_PASSWORD="$password" \
  WAYLAND_VNC_TEMPLATE_TARGET="$target" WAYLAND_VNC_TEMPLATE_KEY="$server_key" \
  python3 scripts/kvm/render-user-data.py "$template" "$tmp/user-data"
printf 'instance-id: wayland-vnc-%s\nlocal-hostname: wayland-vnc-%s\n' "$target" "$target" >"$tmp/meta-data"
genisoimage -quiet -output "$seed" -volid cidata -joliet -rock \
  "$tmp/user-data" "$tmp/meta-data"

echo "== UEFI vars =="
cp /usr/share/OVMF/OVMF_VARS_4M.fd "$vars"

echo "== boot $target guest (headless, virtio-gpu KMS, 9p repo share) =="
# ICH9-LPC S3 enabled so the guest can suspend/resume for that scenario. The
# virtio-gpu output is sized to the largest mode a scenario asks for (4K): the driver
# refuses modes larger than the configured output, and the fixture starts at 1080p.
# -vga none: without it QEMU adds its default stdvga as a SECOND DRM device, both
# connectors are called "Virtual-N" in an order that changes from boot to boot, and a
# guest whose Virtual-1 was the small stdvga head could not take the 4K mode.
qemu-system-x86_64 \
  -name "wayland-vnc-$target" \
  -enable-kvm -machine q35,accel=kvm -cpu host -smp 4 -m 4096 -vga none \
  -global ICH9-LPC.disable_s3=0 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive if=pflash,format=raw,file="$vars" \
  -drive if=virtio,format=qcow2,file="$overlay" \
  -drive if=virtio,format=raw,file="$seed",readonly=on \
  "${gpu[@]}" \
  -netdev user,id=n0,hostfwd=tcp:"$bind":"$port"-:5900 \
  -device virtio-net-pci,netdev=n0 \
  -fsdev local,id=repo,path="$PWD",security_model=none,readonly=on \
  -device virtio-9p-pci,fsdev=repo,mount_tag=repo \
  -chardev socket,path="$work/qga.sock",server=on,wait=off,id=qga0 \
  -device virtio-serial \
  -device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0 \
  -qmp unix:"$work/qmp.sock",server=on,wait=off \
  -serial file:"$work/console.log" \
  -display none -daemonize -pidfile "$work/qemu.pid"

# What the runner needs to reach this guest, next to its sockets.
printf '%s\n' "$port" >"$work/port"
echo "guest booting; console: $work/console.log; QMP: $work/qmp.sock"
echo "the fixture's VNC server will appear on $bind:$port once cloud-init finishes."
