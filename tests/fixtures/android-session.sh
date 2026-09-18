#!/usr/bin/env sh
set -eu
adb="$ANDROID_SDK_ROOT/platform-tools/adb"
emulator="$ANDROID_SDK_ROOT/emulator/emulator"
"$adb" -L tcp:5037 nodaemon server &
adb_pid=$!
emulator_pid=
cleanup() {
  if [ -n "$emulator_pid" ]; then
    kill "$emulator_pid" 2>/dev/null || true
  fi
  kill "$adb_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
"$emulator" "$@" &
emulator_pid=$!
wait "$emulator_pid"
