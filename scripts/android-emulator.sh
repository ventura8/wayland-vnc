#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
container_name=wayland-vnc-android-lab
avd_name=wayland-vnc-api36
# The isolated lab's directory (the AVD and the lab's own adb keys); overridable so the
# tests run this script against a scratch copy and never the real lab.
android_root=${WAYLAND_VNC_ANDROID_ROOT:-$PWD/artifacts/android}
action=${1:-}
case "$action" in
start)
  if [[ $# -lt 2 || $# -gt 3 || (${3:-} != "" && ${3:-} != "--wipe-data") ]]; then
    echo "Usage: $0 start /path/to/Android/Sdk [--wipe-data]" >&2
    exit 2
  fi
  android_sdk=$(realpath -- "$2")
  emulator="$android_sdk/emulator/emulator"
  adb="$android_sdk/platform-tools/adb"
  avd_root="$android_root/avd"
  android_user="$android_root/user"
  if [[ ! -x "$emulator" || ! -x "$adb" ||
    ! -d "$avd_root/$avd_name.avd" ]]; then
    echo "Missing emulator, adb, or isolated AVD; run scripts/android-lab.py first" >&2
    exit 2
  fi
  mkdir -p -- "$android_user"
  if [[ ! -f "$android_user/adbkey" ]]; then
    "$adb" keygen "$android_user/adbkey"
  fi
  chmod 600 -- "$android_user/adbkey"
  chmod 644 -- "$android_user/adbkey.pub"
  if docker container inspect "$container_name" >/dev/null 2>&1; then
    echo "Android lab container already exists" >&2
    exit 2
  fi
  # A lab that was stopped from outside (a plain `docker stop`, a reboot) leaves the
  # AVD's lock files behind, and the next boot refuses with "Running multiple
  # emulators with the same AVD". They are cleared only when no emulator anywhere on
  # this machine is running the AVD, so a live one is never pulled out from under.
  # The emulator takes the AVD as `-avd NAME` or `@NAME`; either one counts.
  if pgrep -f -- "(-avd |@)$avd_name( |$)" >/dev/null; then
    echo "an emulator is already running $avd_name; stop it first" >&2
    exit 2
  fi
  rm -rf -- "$avd_root/$avd_name.avd/multiinstance.lock" \
    "$avd_root/$avd_name.avd/hardware-qemu.ini.lock" \
    "$avd_root/$avd_name.avd"/snapshot.lock*
  # The console token. The emulator writes a fresh one into its home when it finds
  # none, and the container's home dies with it, so the host's adb -- which answers
  # `adb emu` (gestures, rotation, the runner's readiness check) with the token in
  # ~/.emulator_console_auth_token -- was refused every time. Both sides now use the
  # host's token: created here if missing, handed in read-only.
  token="$HOME/.emulator_console_auth_token"
  if [[ ! -s "$token" ]]; then
    (umask 077 && head -c 12 /dev/urandom | base64 | tr -d '/+=' >"$token")
  fi
  kvm_group=$(stat -c %g /dev/kvm)
  lab_uid=$(id -u)
  lab_gid=$(id -g)
  emulator_extra=()
  if [[ ${3:-} == "--wipe-data" ]]; then
    emulator_extra+=("-wipe-data")
  fi
  docker run --rm -d --name "$container_name" --network host \
    --device /dev/kvm --user "$lab_uid:$lab_gid" --group-add "$kvm_group" \
    --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs "/lab-home:uid=$lab_uid,gid=$lab_gid,mode=700" -e HOME=/lab-home \
    -v "$token:/lab-home/.emulator_console_auth_token:ro" \
    -v "$android_sdk:$android_sdk:ro" -v "$android_root:$android_root" \
    -e "ANDROID_AVD_HOME=$avd_root" -e "ANDROID_USER_HOME=$android_user" \
    -e "ANDROID_EMULATOR_HOME=$android_user" -e "ADB_VENDOR_KEYS=$android_user/adbkey" \
    -e "ANDROID_SDK_ROOT=$android_sdk" wayland-vnc-android:dev \
    -avd "$avd_name" \
    -no-window -no-audio -no-snapshot -no-boot-anim -gpu swiftshader \
    -accel on -ports 5554,5555 -memory 2048 "${emulator_extra[@]}"
  echo "Use $adb -s 127.0.0.1:5555 wait-for-device"
  ;;
stop)
  [[ $# == 1 ]] || {
    echo "Usage: $0 stop" >&2
    exit 2
  }
  # Only the lab's own emulator is ever stopped: with no lab container running there
  # is nothing to stop, and the console at emulator-5554 may be another emulator's.
  # While the container runs it holds ports 5554/5555 on the host network, so the
  # emulator answering there is the lab's.
  if [[ "$(docker container inspect -f '{{.State.Running}}' "$container_name" 2>/dev/null)" != true ]]; then
    echo "no running $container_name container; nothing to stop"
    exit 0
  fi
  # Through the console first, so the emulator shuts down cleanly and removes its own
  # AVD locks; `docker stop` only if it has not gone within a minute.
  adb="${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}/platform-tools/adb"
  if [[ -x "$adb" ]]; then
    ANDROID_USER_HOME="$android_root/user" \
      timeout 30 "$adb" -s emulator-5554 emu kill >/dev/null 2>&1 || true
  fi
  for _ in $(seq 1 30); do
    docker container inspect "$container_name" >/dev/null 2>&1 || exit 0
    sleep 2
  done
  docker stop "$container_name"
  ;;
status)
  [[ $# == 1 ]] || {
    echo "Usage: $0 status" >&2
    exit 2
  }
  docker ps --filter "name=^/${container_name}$" --format '{{.Status}}'
  ;;
*)
  echo "Usage: $0 {start /path/to/Android/Sdk [--wipe-data]|stop|status}" >&2
  exit 2
  ;;
esac
