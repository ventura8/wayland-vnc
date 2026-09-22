#!/usr/bin/env bash
# Run the unattended actual-RealVNC scenario suite for every buildable target through
# the isolated viewer harness, writing schema-v2 records into a private evidence root.
# Records stay 'incomplete' unless every scenario passes; this is not a release gate.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH
umask 077

evidence=${WAYLAND_VNC_EVIDENCE:-artifacts/qualification-evidence}
credential=${WAYLAND_VNC_CREDENTIAL:-artifacts/desktop-viewer/fixture.conf}
server_key=${WAYLAND_VNC_SERVER_KEY:-artifacts/desktop-viewer/fixture-rsa.pem}
identities=${WAYLAND_VNC_IDENTITIES:-artifacts/desktop-viewer/identities-5902}
viewer_config=${WAYLAND_VNC_VIEWER_CONFIG:-artifacts/desktop-viewer/harness-vncviewer.conf}
connection=${WAYLAND_VNC_CONNECTION:-artifacts/desktop-viewer/qualify-5902.vnc}
port=${WAYLAND_VNC_PORT:-5902}
# The commit the records are bound to. A tree synced to a lab machine has no .git, so
# the caller names it (the SHA of the tree that was synced); the gate matches records
# to the tagged commit, so this must be the one that gets tagged.
commit=${WAYLAND_VNC_COMMIT:-$(git rev-parse HEAD)}
# --android: the actual RealVNC Viewer for Android in the isolated emulator instead
# of the desktop viewer in the harness (docs/android.md); the fixtures are the same.
# --kvm: each target runs in its own KVM guest (scripts/kvm/build-guest.sh) with the
# harness viewer, for the scenarios a container cannot provide (suspend-resume, and
# real DRM outputs). A fresh guest is built per target: one boot is good for exactly
# one run, since virtio-gpu does not come back whole from S3.
viewer=desktop
mode=container
while [[ "${1:-}" == --* ]]; do
  case "$1" in
  --android) viewer=android ;;
  --kvm) mode=kvm ;;
  *)
    echo "usage: $0 [--android|--kvm] [target...]" >&2
    exit 2
    ;;
  esac
  shift
done
targets=("$@")
if ((${#targets[@]} == 0)); then
  if [[ "$mode" == kvm ]]; then
    targets=(hyprland sway wayfire xfce-labwc lxqt-labwc gnome plasma)
  else
    targets=(gnome plasma xfce-labwc lxqt-labwc wayfire sway)
  fi
fi

required=("$credential" "$server_key")
[[ "$viewer" == android ]] || required+=("$identities" "$viewer_config" "$connection")
for missing in "${required[@]}"; do
  [[ -f "$missing" ]] || {
    echo "Missing private input: $missing" >&2
    exit 2
  }
done
viewer_args=(--harness --identities "$identities" --viewer-config "$viewer_config" --connection "$connection")
[[ "$viewer" == desktop ]] || viewer_args=(--viewer android)

# Boot a fresh guest for the target and wait until cloud-init has provisioned it
# (the console says so); prints the guest's forwarded port. The desktop viewer's
# harness reaches the guest on the lab network; the Android app reaches it through
# `adb reverse` to host loopback, so its guest is forwarded there instead.
boot_guest() {
  local target=$1
  local work="artifacts/kvm/$target"
  local bind=(--harness)
  [[ "$viewer" == desktop ]] || bind=()
  if [[ -f "$work/qemu.pid" ]] && kill -0 "$(cat "$work/qemu.pid")" 2>/dev/null; then
    kill "$(cat "$work/qemu.pid")"
    sleep 3
  fi
  bash scripts/kvm/build-guest.sh "$target" "${bind[@]}" >&2 || return 1
  # Wait through the guest agent for the readiness marker, not the serial console:
  # a desktop guest reboots once to load its custom EDID, so the console's "Cloud-init
  # finished" line appears before the fixture exists.
  PYTHONPATH=src python3 scripts/kvm/wait-guest-ready.py "$work" 2700 >&2 || return 1
  cat "$work/port"
}

status=0
for fixture in "${targets[@]}"; do
  echo "=== $fixture ($viewer viewer, $mode) ===" >&2
  target_port=$port
  run_args=("${viewer_args[@]}")
  if [[ "$mode" == kvm ]]; then
    if ! target_port=$(boot_guest "$fixture"); then
      status=1
      continue
    fi
    run_args+=(--kvm "artifacts/kvm/$fixture")
  # The fixture smoke builds this target's image and proves the fixture starts, so a
  # machine that only received the tree qualifies from scratch; a fixture that cannot
  # start here is recorded as such by the smoke, not as failed scenarios.
  elif [[ "${WAYLAND_VNC_SKIP_FIXTURE_SMOKE:-0}" != 1 ]] && ! bash scripts/fixture-smoke.sh "$fixture"; then
    status=1
    echo "$fixture: fixture smoke failed or is blocked on this machine; not qualified here" >&2
    continue
  fi
  if PYTHONPATH=src python3 scripts/qualify-desktop.py --fixture "$fixture" --port "$target_port" \
    "${run_args[@]}" --credential "$credential" --server-key "$server_key" \
    --evidence-root "$evidence" --commit "$commit"; then
    echo "$fixture: all scenarios passed or blocked" >&2
  else
    status=1
    echo "$fixture: some scenarios failed (see the evidence directory)" >&2
  fi
done
exit "$status"
