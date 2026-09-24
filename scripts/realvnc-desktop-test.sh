#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH
umask 077

usage() {
  echo "Usage: $0 --config PRIVATE_CONNECTION.vnc [--output DIR]" >&2
}

connection=
output=artifacts/desktop-viewer
while (($#)); do
  case "$1" in
  --config)
    connection=${2:-}
    shift 2
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

if [[ -z "$connection" || ! -f "$connection" ]]; then
  usage
  exit 2
fi
if [[ $(stat -c '%a' -- "$connection") != 600 ]]; then
  echo "Refusing connection file unless its mode is exactly 600." >&2
  exit 2
fi
if [[ $(stat -c '%u' -- "$connection") != "$(id -u)" ]]; then
  echo "Refusing a connection file not owned by the current user." >&2
  exit 2
fi
if [[ $(realpath -- "$connection") == "$PWD"/* ]] &&
  ! git check-ignore --no-index --quiet -- "$connection"; then
  echo "Refusing a repository-local connection file that is not ignored by Git." >&2
  exit 2
fi

install -d -m 700 -- "$output"
log="$output/viewer.log"
screenshot="$output/framebuffer.png"
viewer_pid=

cleanup() {
  if [[ -n "$viewer_pid" ]] && kill -0 "$viewer_pid" 2>/dev/null; then
    kill -- "-$viewer_pid" 2>/dev/null || true
    wait "$viewer_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

setsid vncviewer -AutoReconnect=0 -EnableUdpRfb=False -ProxyTcpRfb=0 \
  -PasswordStoreOffer=0 -Log='*:stderr:30' -config "$connection" \
  >"$log" 2>&1 &
viewer_pid=$!

deadline=$((SECONDS + 10))
while ((SECONDS < deadline)); do
  kill -0 "$viewer_pid" 2>/dev/null || {
    echo "RealVNC Viewer exited before the first-frame deadline; see $log" >&2
    exit 1
  }
  # Do not request screenshots during the RA2 handshake; wait for the viewer's verdict.
  if grep -q "AuthFailure" "$log"; then
    echo "RealVNC Viewer reported an authentication failure; see $log" >&2
    exit 1
  fi
  if grep -q "Authentication successful" "$log" &&
    vncviewer -screenshot "$viewer_pid" "$screenshot" >/dev/null 2>&1 &&
    [[ -s "$screenshot" ]]; then
    PYTHONPATH=src python3 scripts/assert-scene.py "$screenshot" >"$output/evidence.json"
    # The EXIT trap ends the viewer's process group (that is this runner's contract),
    # so its pid is not reported: it would be dead by the time anyone read it.
    printf 'screenshot=%s\nlog=%s\n' "$screenshot" "$log"
    exit 0
  fi
  sleep 0.25
done

echo "No RealVNC screenshot within 10 seconds; see $log" >&2
# Tear the viewer down before reporting the failure. The EXIT trap would do it anyway,
# and cleanup is guarded so running twice is a no-op; calling it here also keeps the
# invocation visible, which a trap alone does not always make clear.
cleanup
exit 1
