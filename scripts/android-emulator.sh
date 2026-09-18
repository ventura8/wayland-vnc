#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
container_name=wayland-vnc-android-lab
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
  android_root="$PWD/artifacts/android"
  avd_root="$android_root/avd"
  android_user="$android_root/user"
  if [[ ! -x "$emulator" || ! -x "$adb" ||
    ! -d "$avd_root/wayland-vnc-api36.avd" ]]; then
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
    -v "$android_sdk:$android_sdk:ro" -v "$android_root:$android_root" \
    -e "ANDROID_AVD_HOME=$avd_root" -e "ANDROID_USER_HOME=$android_user" \
    -e "ANDROID_EMULATOR_HOME=$android_user" -e "ADB_VENDOR_KEYS=$android_user/adbkey" \
    -e "ANDROID_SDK_ROOT=$android_sdk" wayland-vnc-android:dev \
    -avd wayland-vnc-api36 \
    -no-window -no-audio -no-snapshot -no-boot-anim -gpu swiftshader \
    -accel on -ports 5554,5555 -memory 2048 "${emulator_extra[@]}"
  echo "Use $adb -s 127.0.0.1:5555 wait-for-device"
  ;;
stop)
  [[ $# == 1 ]] || {
    echo "Usage: $0 stop" >&2
    exit 2
  }
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
