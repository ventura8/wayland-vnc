#!/usr/bin/env bash
# Boot the ISOLATED wayland-vnc Android AVD and wire the ADB path to a host WayVNC
# port. This script never signs into an account and never installs an app: the
# viewer is installed either through the Play Store (a disposable qualification
# account, never a personal one, in the emulator window) or, since 2026-09-17, with
# scripts/android-provision-viewer.sh from any source -- a mirror included -- which
# refuses any APK not carrying RealVNC's own release signature. See docs/android.md.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH

sdk=${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}
avd_name=wayland-vnc-api36
wayvnc_port=${WAYLAND_VNC_KVM_PORT:-5910} # host port a WayVNC fixture listens on
emu_port=${ANDROID_EMULATOR_PORT:-5554}
serial="emulator-${emu_port}"

root="$PWD/artifacts/android"
export ANDROID_SDK_ROOT="$sdk"
export ANDROID_AVD_HOME="$root/avd"
export ANDROID_USER_HOME="$root/user"
adb="$sdk/platform-tools/adb"
emulator="$sdk/emulator/emulator"

[[ -d "$ANDROID_AVD_HOME/$avd_name.avd" ]] || {
  echo "Isolated AVD missing; run: python3 scripts/android-lab.py --sdk '$sdk'" >&2
  exit 2
}

echo "== boot isolated AVD ($avd_name) =="
if "$adb" devices | grep -q "^$serial"; then
  # Whatever answers on this port is reused for qualification and gets the reverse
  # port, so it must be THIS isolated AVD: an operator's own emulator, with their
  # own accounts in it, is not a qualification device. The console answers the AVD
  # name on its first line; anything else, including no answer, stops here.
  attached=$("$adb" -s "$serial" emu avd name 2>/dev/null | tr -d '\r' | sed -n 1p || true)
  if [[ "$attached" != "$avd_name" ]]; then
    echo "the emulator at $serial is not the isolated AVD $avd_name" \
      "(it reports '${attached:-nothing}'); stop it, or pick another ANDROID_EMULATOR_PORT" >&2
    exit 1
  fi
  echo "  emulator already attached at $serial ($avd_name)"
else
  mkdir -p "$root"
  nohup "$emulator" -avd "$avd_name" -port "$emu_port" \
    -gpu swiftshader_indirect -no-audio -no-snapshot-load -no-boot-anim \
    -accel on -netdelay none -netspeed full \
    >"$root/emulator.log" 2>&1 &
  echo "  emulator pid $! (log: $root/emulator.log)"
fi

echo "== wait for device =="
# Bounded like every other wait here: a failed emulator start would otherwise hang
# this script for ever instead of pointing at the log that says why.
timeout 180 "$adb" -s "$serial" wait-for-device || {
  echo "emulator did not present a device within 180s; see $root/emulator.log" >&2
  exit 1
}
# A fresh AVD may hold adb in 'unauthorized' until the "Allow USB debugging" dialog
# is accepted in the emulator window. That tap is the user's, like the Play sign-in.
for _ in $(seq 1 60); do
  state=$("$adb" -s "$serial" get-state 2>/dev/null | tr -d '\r' || true)
  [[ "$state" = "device" ]] && break
  if "$adb" devices | grep -q "^${serial}[[:space:]]*unauthorized"; then
    echo "  waiting: accept 'Allow USB debugging' in the emulator window..."
  fi
  sleep 3
done
[[ "$("$adb" -s "$serial" get-state 2>/dev/null | tr -d '\r')" = "device" ]] || {
  echo "adb is not authorized; accept the 'Allow USB debugging' prompt and re-run" >&2
  exit 1
}
echo "== wait for boot_completed =="
for _ in $(seq 1 120); do
  [[ "$("$adb" -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]] && break
  sleep 2
done
[[ "$("$adb" -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]] || {
  echo "emulator did not finish booting" >&2
  exit 1
}
echo "  booted: $("$adb" -s "$serial" shell getprop ro.build.version.release | tr -d '\r') (API $("$adb" -s "$serial" shell getprop ro.build.version.sdk | tr -d '\r'))"

echo "== quiet the device for automation =="
# No soft keyboard in the input path. With the AVD's hardware keyboard present, Gboard
# takes physical key events for itself and intermittently swallows the ones
# `adb shell input` injects: the field is focused, the dispatcher delivers the event to
# the right window, and no text ever appears -- after a fresh boot it works, a few
# connections later it does not. The driver injects every key itself, so the lab device
# needs no input method at all. Disabling is persistent and idempotent.
# Taking an input method off the enabled list is not enough: a keyboard process
# already bound to a field stays bound, and Gboard in that state logs "Ignore ... due
# to stale request" and drops what it is given. The package itself is disabled, so
# nothing can bind; the enabled list is then emptied for anything left.
"$adb" -s "$serial" shell pm disable-user --user 0 com.google.android.inputmethod.latin >/dev/null 2>&1 || true
while read -r ime; do
  [[ -n "$ime" ]] && "$adb" -s "$serial" shell ime disable "$ime" >/dev/null
done < <("$adb" -s "$serial" shell ime list -s 2>/dev/null | tr -d '\r')
# Play services' autofill proxy registers as an input method but is not a keyboard,
# cannot be disabled, and binds only when autofill asks; everything else must be gone.
remaining=$("$adb" -s "$serial" shell ime list -s 2>/dev/null | tr -d '\r' |
  grep -v '/.autofill.service.AutofillInputMethodServiceProxy$' || true)
[[ -z "$remaining" ]] || {
  echo "keyboards still enabled after disabling them all: $remaining" >&2
  exit 1
}
echo "  no keyboard enabled: injected keys reach the focused field directly"
# Sheets and dialogs that are still sliding in are tapped where the UI dump says they
# will be, not where they are; with animations off they are there at once.
for scale in window_animation_scale transition_animation_scale animator_duration_scale; do
  "$adb" -s "$serial" shell settings put global "$scale" 0
done
echo "  animations off"

echo "== wire the VNC path (emulator 127.0.0.1:5900 -> host :$wayvnc_port) =="
"$adb" -s "$serial" reverse tcp:5900 "tcp:${wayvnc_port}" >/dev/null
echo "  adb reverse set: inside the app, connect to 127.0.0.1:5900"
"$adb" -s "$serial" reverse --list | sed 's/^/  /'

is_installed=$("$adb" -s "$serial" shell pm list packages com.realvnc.viewer.android 2>/dev/null | tr -d '\r')
echo
echo "=== STAGED — hand-off to you ==="
if [[ -n "$is_installed" ]]; then
  echo "RealVNC Viewer is already installed ($is_installed)."
  echo "You can launch it and connect to 127.0.0.1:5900."
else
  echo "RealVNC Viewer is NOT installed yet. Either:"
  echo "  - scripts/android-provision-viewer.sh --url <apk url>  (or --apk <file>): any"
  echo "    source, a mirror included; refused unless signed by RealVNC Ltd's pinned key; or"
  echo "  - in the emulator window, sign in to the Play Store with a DISPOSABLE"
  echo "    qualification account (never your personal one) and install"
  echo "    'RealVNC Viewer: Remote Desktop' (com.realvnc.viewer.android)."
  echo "  Then: scripts/qualify-desktop.py --viewer android ... drives the app."
fi
echo
echo "Note: this AVD is isolated via ANDROID_AVD_HOME/ANDROID_USER_HOME under"
echo "      artifacts/android. A plain 'adb devices' that does not export those will"
echo "      present a different adb key and report the device as 'unauthorized'."
echo "      Use this script, or export the same two variables, when talking to it."
echo "This script entered no account credentials and installed no APK; any Play Store"
echo "sign-in happened in the emulator window, by you, with the disposable account."
