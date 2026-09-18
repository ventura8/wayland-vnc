#!/usr/bin/env bash
# Start one fixture on a loopback port and open the installed RealVNC Viewer through a
# private connection file, so a person only has to type, click, scroll and drag.
# The captured screenshot is merged into a record with record-manual-input.py.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH
umask 077

usage() {
  echo "Usage: $0 start FIXTURE PORT CONNECTION.vnc CREDENTIAL [SERVER_KEY]" >&2
  echo "       $0 capture VIEWER_PID OUTPUT.png" >&2
  echo "       $0 stop FIXTURE [VIEWER_PID]" >&2
}

case "${1:-}" in
start)
  fixture=${2:?}
  port=${3:?}
  connection=${4:?}
  credential=${5:?}
  key=${6:-}
  container="wayland-vnc-$fixture-attended"
  docker rm -f -- "$container" >/dev/null 2>&1 || true
  args=(docker run -d --name "$container" --cap-drop ALL --security-opt no-new-privileges
    -p "127.0.0.1:$port:5900" -v "$(realpath -- "$credential"):/run/secrets/fixture.conf:ro"
    -e WAYLAND_VNC_CREDENTIAL_FILE=/run/secrets/fixture.conf)
  if [[ -n "$key" ]]; then
    args+=(-v "$(realpath -- "$key"):/run/secrets/rsa.pem:ro" -e WAYLAND_VNC_SERVER_KEY_FILE=/run/secrets/rsa.pem)
  fi
  if [[ "$fixture" == plasma ]]; then
    node=${WAYLAND_VNC_RENDER_NODE:-/dev/dri/renderD128}
    args+=(--device "$node" --group-add "$(stat -c '%g' -- "$node")")
  fi
  "${args[@]}" "wayland-vnc-$fixture:dev" >/dev/null
  # From here until the success line below, any failure (smoke check, viewer never
  # authenticating) must not leave the container -- or a half-started viewer -- behind
  # for the operator to find; `stop` is only ever told about a successful start.
  pid=
  abort() {
    if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
    docker rm -f -- "$container" >/dev/null 2>&1 || true
  }
  trap abort EXIT
  timeout 120 docker exec -e PYTHONPATH=/fixture "$container" python3 -m wayland_vnc.fixture_smoke \
    --fixture "$fixture" --timeout 90 >/dev/null
  options=()
  if [[ "$fixture" == gnome ]]; then options=(-VerifyId=0 -WarnUnencrypted=0); fi
  log="artifacts/desktop-viewer/attended-$fixture.log"
  mkdir -p -- "$(dirname -- "$log")"
  setsid vncviewer -AutoReconnect=0 -EnableUdpRfb=False -ProxyTcpRfb=0 -PasswordStoreOffer=0 \
    -Log='*:stderr:30' "${options[@]}" -config "$connection" >"$log" 2>&1 &
  pid=$!
  for _ in $(seq 1 40); do
    grep -q "Authentication successful" "$log" && break
    sleep 0.25
  done
  grep -q "Authentication successful" "$log" || {
    echo "viewer did not authenticate; see $log" >&2
    exit 1
  }
  trap - EXIT
  echo "viewer_pid=$pid container=$container"
  ;;
capture)
  pid=${2:?}
  output=${3:?}
  vncviewer -screenshot "$pid" "$output"
  PYTHONPATH=src python3 scripts/assert-scene.py --require-input --require-gestures "$output"
  ;;
stop)
  fixture=${2:?}
  # A bare `pkill -x vncviewer` also kills the operator's own viewer sessions, which
  # are nothing to do with this fixture. Stop only the PID `start` reported.
  viewer_pid=${3:-}
  if [[ -n "$viewer_pid" ]]; then
    kill "$viewer_pid" 2>/dev/null || true
  fi
  docker rm -f -- "wayland-vnc-$fixture-attended" >/dev/null 2>&1 || true
  ;;
*)
  usage
  exit 2
  ;;
esac
