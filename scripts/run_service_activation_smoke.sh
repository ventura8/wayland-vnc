#!/usr/bin/env bash
# Prove the installer enables AND activates the service under a real systemd.
#
# The other package smokes run without a live systemd, where the postinst's
# _systemd_is_live guard correctly skips activation. This smoke runs systemd as PID 1
# in a throwaway container, installs the real .deb so the real postinst runs, and
# asserts the outcome per user:
#   * a logged-in user WITH a stored credential  -> unit enabled and ACTIVE, using it
#   * a logged-in user WITHOUT a credential      -> unit enabled and ACTIVE too: serve
#                                                  generates a random 600 credential,
#                                                  so it never runs unauthenticated
#   * after purge                                -> nothing enabled, nothing running
# A stand-in `wayvnc` that just sleeps lets the unit reach `active` without a
# compositor; what is under test is the installer's activation, not WayVNC itself.
# Never touches the host: everything happens inside one privileged, --rm container.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH
mkdir -p reports/distro-logs artifacts/deb

IMAGE="wayland-vnc-systemd-smoke:local"
NAME="wayland-vnc-systemd-$$"

echo "=== build a systemd-as-PID1 image (ubuntu:26.04) ==="
docker build -q -t "$IMAGE" - <<'DOCKERFILE' >/dev/null
FROM ubuntu:26.04@sha256:da6fc2be547864451aa253836dd926da33623312df4a9a243e35dc877c378a78
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    systemd systemd-sysv dbus-user-session dbus-daemon \
    build-essential debhelper dpkg-dev python3 python3-pil openssl \
    && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
DOCKERFILE

trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

echo "=== boot systemd in the container ==="
docker run -d --rm --name "$NAME" --privileged --cgroupns=host \
  --tmpfs /run --tmpfs /run/lock -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v "$PWD:/src:ro" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
  state=$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)
  case "$state" in
  running | degraded) break ;;
  *) ;; # not up yet; keep waiting
  esac
  sleep 1
done
echo "  systemd state: ${state:-unknown}"
case "${state:-}" in
running | degraded) ;;
*)
  # Everything after this asserts what systemd did with the unit. Without a live
  # systemd those assertions would fail for the wrong reason, or worse, pass.
  echo "systemd did not start in the container (state: ${state:-unknown})" >&2
  exit 1
  ;;
esac

runner=$(mktemp)
cat >"$runner" <<'INNER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
mkdir -p /build && cp -a /src/. /build/ && cd /build
rm -rf debian/wayland-vnc debian/.debhelper debian/files ../wayland-vnc_*.deb 2>/dev/null || true

echo "== build the .deb =="
DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -b -us -uc >/dev/null 2>&1
deb=$(ls ../wayland-vnc_*_all.deb | head -1)
echo "  built $(basename "$deb")"

# A stand-in wayvnc so the unit can reach 'active' with no compositor present;
# the installer's activation is what is under test here, not WayVNC.
cat >/usr/local/bin/wayvnc <<'FAKE'
#!/bin/sh
echo "fake wayvnc argv: $*"
exec sleep infinity
FAKE
chmod +x /usr/local/bin/wayvnc
# The capability probe reads wayland-info, and serve refuses outright when it selects
# no backend rather than falling through to WayVNC. This container has no compositor,
# so stand in for one and advertise the wlroots protocols WayVNC needs; without this
# the unit fails on "Refusing to serve" and never reaches active.
cat >/usr/local/bin/wayland-info <<'FAKE'
#!/bin/sh
cat <<'OUT'
interface: 'zwlr_screencopy_manager_v1', version: 3, name: 10
interface: 'zwlr_virtual_pointer_manager_v1', version: 2, name: 11
interface: 'zwp_virtual_keyboard_manager_v1', version: 1, name: 12
OUT
FAKE
chmod +x /usr/local/bin/wayland-info
# Satisfy the real .deb dependency without pulling the real server.
cat >/tmp/wayvnc-shim.control <<'CTL'
Package: wayvnc
Version: 0.9.1-shim
Architecture: all
Maintainer: smoke
Description: stand-in that satisfies the dependency for this smoke only
CTL
mkdir -p /tmp/shim/DEBIAN && cp /tmp/wayvnc-shim.control /tmp/shim/DEBIAN/control
dpkg-deb -b /tmp/shim /tmp/wayvnc-shim.deb >/dev/null && dpkg -i /tmp/wayvnc-shim.deb >/dev/null

echo "== two logged-in users: 'ready' stored a credential, 'bare' did not =="
for u in ready bare; do
  useradd -m -s /bin/bash "$u"
  loginctl enable-linger "$u"
done
for _ in $(seq 1 30); do
  [ -S "/run/user/$(id -u ready)/bus" ] && [ -S "/run/user/$(id -u bare)/bus" ] && break
  sleep 1
done
test -S "/run/user/$(id -u ready)/bus" || { echo "no user bus for 'ready'" >&2; exit 1; }
echo "  ok: both user managers are up (bus sockets present)"
# The unit requires WAYLAND_DISPLAY in the user manager's environment; a real login
# session exports it, so set it the way a session would.
for u in ready bare; do
  uid=$(id -u "$u")
  runuser -u "$u" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    systemctl --user set-environment WAYLAND_DISPLAY=wayland-0
done
# Only 'ready' provisions a credential -- BEFORE the package installs. Run as that
# user so the config directory is created with the right owner (a root-made
# ~/.config would block the user from writing into it).
printf 'smokepw1\nsmokepw1\n' | runuser -u ready -- env PYTHONPATH=/build/src \
  python3 -m wayland_vnc set-password --username vnc --stdin
echo "  ok: 'ready' stored a credential; 'bare' has none"

echo "== install the real .deb: the real postinst must enable AND activate =="
apt-get install -y "$deb" >/dev/null 2>&1
ustate() {
  uid=$(id -u "$1")
  runuser -u "$1" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    systemctl --user "$2" wayland-vnc.service 2>/dev/null || true
}
test "$(systemctl --global is-enabled wayland-vnc.service)" = enabled \
  || { echo "not globally enabled" >&2; exit 1; }
echo "  ok: --global enable applied"

echo "== happy: the user with a credential is enabled and ACTIVE =="
for _ in $(seq 1 15); do [ "$(ustate ready is-active)" = active ] && break; sleep 1; done
test "$(ustate ready is-enabled)" = enabled || { echo "'ready' not enabled" >&2; exit 1; }
test "$(ustate ready is-active)" = active || {
  echo "'ready' not active: $(ustate ready is-active)" >&2
  runuser -u ready -- env XDG_RUNTIME_DIR="/run/user/$(id -u ready)" \
    journalctl --user -u wayland-vnc.service --no-pager | tail -20 >&2
  exit 1
}
echo "  ok: 'ready' -> is-enabled=enabled, is-active=active"
# Mandatory, not a courtesy line: under Type=simple with RemainAfterExit the unit
# reports 'active' the instant serve starts, so 'active' alone proved nothing when
# serve was crash-looping. Reaching exec is the evidence that it served. It is
# also not instant: serve probes the session and provisions first, so the journal
# is polled rather than read once (a single read raced it on a slow CI runner).
reached_exec() {
  runuser -u "$1" -- env XDG_RUNTIME_DIR="/run/user/$(id -u "$1")" \
    journalctl --user -u wayland-vnc.service --no-pager 2>/dev/null | grep -q -- "--config"
}
for _ in $(seq 1 30); do reached_exec ready && break; sleep 1; done
reached_exec ready || {
  echo "'ready' never reached exec; unit journal follows" >&2
  runuser -u ready -- env XDG_RUNTIME_DIR="/run/user/$(id -u ready)" \
    journalctl --user -u wayland-vnc.service --no-pager | tail -25 >&2
  exit 1
}
echo "  ok: the unit exec'd wayvnc --config <loopback config>"

echo "== happy: the user WITHOUT a credential is ALSO active, on a generated one =="
for _ in $(seq 1 15); do [ "$(ustate bare is-active)" = active ] && break; sleep 1; done
test "$(ustate bare is-enabled)" = enabled || { echo "'bare' not enabled" >&2; exit 1; }
test "$(ustate bare is-active)" = active || {
  echo "'bare' not active: $(ustate bare is-active)" >&2
  runuser -u bare -- env XDG_RUNTIME_DIR="/run/user/$(id -u bare)" \
    journalctl --user -u wayland-vnc.service --no-pager | tail -20 >&2
  exit 1
}
# Type=simple reports 'active' as soon as serve starts, while it is still generating
# the RSA key; wait for provisioning to finish before inspecting what it wrote.
conf=/home/bare/.config/wayland-vnc/wayvnc.conf
for _ in $(seq 1 30); do [ -f "$conf" ] && break; sleep 1; done
gen=/home/bare/.config/wayland-vnc/credentials
test -f "$gen" && test "$(stat -c '%a' "$gen")" = 600 \
  || { echo "'bare' has no generated 600 credential" >&2; exit 1; }
test -f "$conf" || {
  # Explain, not just fail: what serve logged for this user is the only clue.
  echo "'bare' never got a provisioned config; unit journal follows" >&2
  runuser -u bare -- env XDG_RUNTIME_DIR="/run/user/$(id -u bare)" \
    journalctl --user -u wayland-vnc.service --no-pager | tail -25 >&2
  exit 1
}
grep -q '^enable_auth=true$' "$conf" || { echo "'bare' config lacks auth" >&2; exit 1; }
grep -q '^address=127.0.0.1$' "$conf" || { echo "'bare' config is not loopback-only by default" >&2; exit 1; }
echo "  ok: 'bare' -> enabled, active, on a generated mode-600 credential with auth on"

echo "== purge: nothing enabled, nothing running =="
apt-get purge -y wayland-vnc >/dev/null 2>&1
test "$(systemctl --global is-enabled wayland-vnc.service 2>/dev/null || echo gone)" != enabled \
  || { echo "still globally enabled after purge" >&2; exit 1; }
test "$(ustate ready is-active)" != active || { echo "still active after purge" >&2; exit 1; }
echo "  ok: purge disabled the unit and stopped the service"
echo "SERVICE ACTIVATION SMOKE PASSED"
INNER

docker cp "$runner" "$NAME:/runner.sh"
rm -f "$runner"
status=0
docker exec "$NAME" bash /runner.sh 2>&1 | tee reports/distro-logs/service-activation-smoke.log || status=1
exit "$status"
