#!/bin/sh
# Snap user-daemon entry point for `wayland-vnc serve`.
#
# snapd has no equivalent of the systemd unit's ConditionEnvironment=WAYLAND_DISPLAY
# or of its start limit, so a bare `serve` on a host without a native Wayland session
# fails, gets restarted, fails again, and keeps doing that for as long as the machine
# is on. serve's refusal is a permanent fact about the session, not a transient error,
# so this exits 0 for it: snapd then leaves the daemon alone until the next start.
#
# A session that is merely not ready yet IS transient -- the daemon can start before
# the compositor has exported WAYLAND_DISPLAY into the user's environment, or the
# compositor may never export it at all -- so an unset variable is never taken as
# evidence of a non-Wayland session. The compositor's own socket in the runtime
# directory is: when one appears, WAYLAND_DISPLAY is derived from it. The daemon
# gives up (exit 0) only once logind confirms that none of this user's sessions is a
# Wayland one; until either is known it keeps waiting, which costs nothing. A logind
# that cannot be asked at all (no loginctl, or a failing query) is not that
# confirmation: the failure is reported once, and the wait is then bounded, since
# without logind nothing would ever end it. Anything serve itself reports stays a
# failure, and snapd's on-failure restart applies to it.
set -eu

# How long to keep waiting for a compositor socket once logind has proved unaskable.
LOGIND_UNAVAILABLE_LIMIT=600
logind_unavailable=

wayland_socket() {
  # The compositor's display socket: wayland-0, wayland-1 ... but never their .lock.
  for candidate in "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"/wayland-[0-9]*; do
    case "$candidate" in *.lock) continue ;; esac
    if [ -S "$candidate" ]; then
      basename -- "$candidate"
      return 0
    fi
  done
  return 1
}

no_wayland_session() {
  # True only on logind's word that this user has sessions and none is Wayland.
  # A query that fails is not that word: it is said once, and marks logind unaskable.
  if ! command -v loginctl >/dev/null 2>&1; then
    [ -n "$logind_unavailable" ] || echo "wayland-vnc: loginctl is not available." >&2
    logind_unavailable=yes
    return 1
  fi
  if ! listing=$(loginctl list-sessions --no-legend 2>&1); then
    [ -n "$logind_unavailable" ] ||
      echo "wayland-vnc: cannot ask logind about sessions: $listing" >&2
    logind_unavailable=yes
    return 1
  fi
  logind_unavailable=
  sessions=$(printf '%s\n' "$listing" | awk -v user="$(id -un)" '$3 == user { print $1 }')
  [ -n "$sessions" ] || return 1
  for session in $sessions; do
    if [ "$(loginctl show-session "$session" -p Type --value 2>/dev/null)" = wayland ]; then
      return 1
    fi
  done
  return 0
}

waited=0
while [ -z "${WAYLAND_DISPLAY:-}" ]; do
  if socket=$(wayland_socket); then
    export WAYLAND_DISPLAY="$socket"
    break
  fi
  if no_wayland_session; then
    echo "wayland-vnc: logind reports no Wayland session for this user; not serving." >&2
    exit 0
  fi
  if [ -n "$logind_unavailable" ] && [ "$waited" -ge "$LOGIND_UNAVAILABLE_LIMIT" ]; then
    echo "wayland-vnc: no compositor socket appeared in ${waited}s and logind could not" \
      "be asked; not serving." >&2
    exit 0
  fi
  sleep 2
  waited=$((waited + 2))
done

exec "${SNAP:-/snap/wayland-vnc/current}/usr/bin/wayland-vnc" serve
