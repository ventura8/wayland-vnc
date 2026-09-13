#!/usr/bin/env bash
# Platform-agnostic post-install scenario suite for an installed wayland-vnc
# package. Run INSIDE a container after the package is installed; it exercises
# every happy and failure path of the diagnostic, provisioning, and serving CLI
# without a real compositor or WayVNC (serve is proven to fail closed, then to
# reach exec with a fake wayvnc on PATH). It never opens a network listener.
#
# It does not install or remove the package itself — the per-format smoke driver
# does that around this script so uninstall/purge assertions live with the
# package manager that owns them.
set -euo pipefail

fail() {
  echo "SCENARIO FAIL: $*" >&2
  exit 1
}
ok() { echo "  ok: $*"; }

CLI=${WAYLAND_VNC_CLI:-wayland-vnc}
work=$(mktemp -d)
export WAYLAND_VNC_CONFIG_DIR="$work/config"
# The scenarios stand in for the compositor with fakes on PATH. The real session bus
# would let the probe find a desktop's Mutter or KWin and serve through that daemon
# instead; on a GNOME development machine that restarts the developer's own server.
export DBUS_SESSION_BUS_ADDRESS="unix:path=$work/no-session-bus"
trap 'rm -rf "$work"' EXIT

echo "== happy: diagnostic =="
# doctor is read-only; exit 0 (candidate) or 2 (none) are both valid, but it must
# print a schema and never error out.
doctor_out=$("$CLI" doctor --json || true)
printf '%s' "$doctor_out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["schema_version"]==1; assert d["qualification"]=="unqualified"' ||
  fail "doctor did not emit a valid unqualified diagnostic"
ok "doctor emits a versioned, unqualified diagnostic"
"$CLI" status >/dev/null 2>&1 || true
ok "status runs read-only"

echo "== happy: provisioning =="
printf 'smokepw1\nsmokepw1\n' | "$CLI" set-password --username vnc --stdin >/dev/null
ok "set-password stored a credential"
test "$(stat -c '%a' "$WAYLAND_VNC_CONFIG_DIR/credentials")" = 600 ||
  fail "credentials file is not mode 600"
ok "credentials file is mode 600"
"$CLI" provision --json >/dev/null
# This machine only by default: a user unit cannot fence a wildcard bind, so the
# wildcard is an explicit choice (the settings app's switch, or --address here).
grep -q '^address=127.0.0.1$' "$WAYLAND_VNC_CONFIG_DIR/wayvnc.conf" ||
  fail "provisioned config does not bind loopback by default"
grep -q '^enable_auth=true$' "$WAYLAND_VNC_CONFIG_DIR/wayvnc.conf" ||
  fail "provisioned config did not enable auth"
test "$(stat -c '%a' "$WAYLAND_VNC_CONFIG_DIR/wayvnc.conf")" = 600 ||
  fail "wayvnc.conf is not mode 600"
ok "provision wrote a loopback-only, authenticated, 0600 config"
"$CLI" provision --json --address 0.0.0.0 >/dev/null
grep -q '^address=0.0.0.0$' "$WAYLAND_VNC_CONFIG_DIR/wayvnc.conf" ||
  fail "the explicit local-network opt-in was not stored"
ok "local network access is an explicit opt-in that provision stores"

echo "== bad: provisioning rejects a privileged port =="
if "$CLI" provision --port 443 >/dev/null 2>&1; then
  fail "provision accepted a privileged port"
fi
ok "provision rejected port 443"

echo "== bad: mismatched password is rejected =="
if printf 'one\ntwo\n' | "$CLI" set-password --stdin >/dev/null 2>&1; then
  fail "set-password accepted mismatched entries"
fi
ok "set-password rejected a mismatch"

echo "== bad: serve refuses an X11 session =="
# A non-zero exit alone would also be produced by an unrelated failure (a missing
# binary, a broken config), so the refusal itself has to be in the output.
x11_out=$(XDG_SESSION_TYPE=x11 "$CLI" serve 2>&1 || true)
case "$x11_out" in
*"native Wayland"*) ;;
*) fail "serve did not refuse X11 with the expected message: $x11_out" ;;
esac
ok "serve refused X11 and said why"

echo "== happy: serve self-provisions a credential when none is stored =="
# Install must leave the service running, so a missing password is generated -- but
# the server still never runs unauthenticated. Prove both with a fake wayvnc that
# records its argv and exits.
empty="$work/empty"
mkdir -p "$work/bin"
cat >"$work/bin/wayvnc" <<'FAKE'
#!/bin/sh
echo "fake wayvnc argv: $*" > "$WAYLAND_VNC_CONFIG_DIR/served"
exit 0
FAKE
chmod +x "$work/bin/wayvnc"
# The capability probe reads wayland-info, and serve now refuses outright when no
# backend is selected rather than falling through to WayVNC. A bare container
# advertises nothing, so stand in for the compositor and report the wlroots capture
# and input protocols WayVNC actually needs; that exercises the real dispatch instead
# of a fall-through that should not exist.
cat >"$work/bin/wayland-info" <<'FAKE'
#!/bin/sh
cat <<'OUT'
interface: 'zwlr_screencopy_manager_v1', version: 3, name: 10
interface: 'zwlr_virtual_pointer_manager_v1', version: 2, name: 11
interface: 'zwp_virtual_keyboard_manager_v1', version: 1, name: 12
OUT
FAKE
chmod +x "$work/bin/wayland-info"
PATH="$work/bin:$PATH" WAYLAND_VNC_CONFIG_DIR="$empty" XDG_SESSION_TYPE=wayland "$CLI" serve \
  >/dev/null 2>"$work/serve.err" || true
test -f "$empty/credentials" || fail "serve did not generate a credential"
test "$(stat -c '%a' "$empty/credentials")" = 600 || fail "generated credential is not 600"
grep -q '^password=.\{6,\}' "$empty/credentials" || fail "generated password is too short"
grep -q '^enable_auth=true$' "$empty/wayvnc.conf" || fail "self-provisioned config lacks auth"
grep -q -- "--config" "$empty/served" || fail "serve did not reach exec after self-provisioning"
grep -q "random one was generated" "$work/serve.err" || fail "user was not told a password was generated"
ok "serve generated a 600 credential, kept auth on, and reached exec"

echo "== happy: serve reaches exec with a fake wayvnc on PATH =="
# Prove the serving path builds the right argv without launching a real server:
# a fake wayvnc records its arguments and exits.
fakebin="$work/bin"
mkdir -p "$fakebin"
cat >"$fakebin/wayvnc" <<'FAKE'
#!/bin/sh
echo "fake wayvnc argv: $*" > "$WAYLAND_VNC_CONFIG_DIR/served"
exit 0
FAKE
chmod +x "$fakebin/wayvnc"
if command -v wayvnc >/dev/null 2>&1; then
  echo "  note: real wayvnc present; using it would open a socket, so the fake shadows it"
fi
# The opt-in scenario above left the wildcard stored; a plain provision puts the
# default back, so what serve hands to wayvnc here really is the loopback config.
"$CLI" provision --json >/dev/null
grep -q '^address=127.0.0.1$' "$WAYLAND_VNC_CONFIG_DIR/wayvnc.conf" ||
  fail "a plain provision did not restore the loopback default"
PATH="$fakebin:$PATH" XDG_SESSION_TYPE=wayland "$CLI" serve >/dev/null 2>&1 || true
grep -q -- "--config" "$WAYLAND_VNC_CONFIG_DIR/served" ||
  fail "serve did not exec wayvnc with a --config argument"
ok "serve execs wayvnc --config <loopback config>"

echo "ALL SCENARIOS PASSED"
