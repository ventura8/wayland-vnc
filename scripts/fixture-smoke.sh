#!/usr/bin/env bash
# Start one isolated compositor fixture, run the in-container smoke checks, clean up.
# Passing proves fixture readiness only; it is never viewer compatibility evidence.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH

usage() {
  echo "Usage: $0 sway|labwc|xfce-labwc|lxqt-labwc|wayfire|hyprland|plasma|gnome [--no-build] [--output DIR]" >&2
}

fixture=${1:-}
case "$fixture" in
sway | labwc | xfce-labwc | lxqt-labwc | wayfire | hyprland | plasma | gnome) ;;
*)
  usage
  exit 2
  ;;
esac
shift
build=1
output=
while (($#)); do
  case "$1" in
  --no-build)
    build=0
    shift
    ;;
  --output)
    output=${2:-}
    shift 2
    ;;
  *)
    usage
    exit 2
    ;;
  esac
done

# The native image keeps its plain tag; a run for the other architecture (emulated
# on a developer machine) tags its own, so it can never replace the native image
# under the qualification runner's feet -- which once put an arm64 fixture into an
# amd64 qualification run.
arch=$(bash scripts/target-arch.sh deb)
platform=$(bash scripts/target-arch.sh platform)
host_arch=$(env -u WAYLAND_VNC_ARCH -u DOCKER_DEFAULT_PLATFORM bash scripts/target-arch.sh deb)
image="wayland-vnc-$fixture:dev"
[ "$arch" = "$host_arch" ] || image="wayland-vnc-$fixture:dev-$arch"
container="wayland-vnc-$fixture-smoke-$$"

cleanup() {
  timeout 30 docker rm -f -- "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

if ((build)); then
  docker build -q --platform "$platform" -f "docker/Dockerfile.$fixture" -t "$image" . >/dev/null
fi

# KWin's PipeWire screencast needs OpenGL, which needs a DRM render node. Only the
# render node is shared; the KMS card nodes that own the host display never are.
device_args=()
if [[ "$fixture" == plasma ]]; then
  render_node=${WAYLAND_VNC_RENDER_NODE:-/dev/dri/renderD128}
  if [[ ! -c "$render_node" ]]; then
    # Exit 3 marks a blocked prerequisite; it is never a pass and never silent.
    echo "BLOCKED: the plasma fixture needs a DRM render node ($render_node is absent)." >&2
    exit 3
  fi
  device_args=(--device "$render_node" --group-add "$(stat -c '%g' -- "$render_node")")
fi

# No published port and no network: the smoke test never exposes the fixture.
docker run -d --name "$container" --platform "$platform" --network none --cap-drop ALL \
  --security-opt no-new-privileges "${device_args[@]}" "$image" >/dev/null

report=$(mktemp)
finish() {
  rm -f -- "$report"
  cleanup
}
trap finish EXIT INT TERM

status=0
timeout 90 docker exec -e PYTHONPATH=/fixture "$container" \
  python3 -m wayland_vnc.fixture_smoke --fixture "$fixture" \
  --width 1920 --height 1080 --timeout 60 >"$report" || status=$?

if [[ -n "$output" ]]; then
  install -d -m 700 -- "$output"
  install -m 600 -- "$report" "$output/fixture-smoke-$fixture.json"
fi
cat -- "$report"
if ((status != 0)); then
  echo "Fixture smoke checks failed for $fixture (status $status)" >&2
  # Redact the generated WayVNC config line if it ever reached the log.
  docker logs --tail 40 -- "$container" 2>&1 | sed 's/password=.*/password=<redacted>/' >&2 || true
  exit 1
fi
