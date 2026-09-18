#!/usr/bin/env bash
# Hardware validation: exercise the packaged product on THIS machine, end to end,
# against the actual RealVNC Viewer. Not a container -- the real desktop, the real
# installer, the real systemd user manager, the real network interface.
#
# Phases, each recorded in artifacts/hardware/<stamp>/report.json:
#   install     apt-installs the built .deb; the real postinst must enable AND start
#   drawer      the desktop entry and both icons are visible to the shell
#   service     the unit is active and port 5900 is served ON LOOPBACK ONLY, the
#               default; records the backend
#   password    a test viewer password is set through the product itself
#   e2e-*       the REAL RealVNC Viewer connects over loopback and the LAN address,
#               authenticates, and a frame of THIS desktop arrives (not a blank one)
#   stream-thread (grd backend only) while the loopback viewer streams, the daemon's
#               PipeWire data loop is an ordinary thread with no RLIMIT_RTTIME: rtkit
#               is real on a desktop, and a realtime data loop copying 4K frames is
#               SIGKILLed by the kernel after 200 ms without a sleep (patch 0008)
#   lan-on      local network access is turned on through the product (the same
#               action as the settings app's switch) and the listener moves to every
#               interface; lan-off turns it back off and the LAN address then refuses
#   bad-password a wrong password is refused by the real server
#   uninstall   purge stops and disables our unit and leaves nothing behind, without
#               touching the desktop's own remote-desktop daemon
#   reinstall   the machine is left installed and running
#
# A phase whose prerequisite cannot be met on this host is recorded `blocked` with the
# reason -- never silently passed. Blocked does not fail the run; failed does.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
umask 077
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
# The real machine is no exception: the Python here runs from the project venv.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH

deb=${WAYLAND_VNC_DEB:-artifacts/deb/wayland-vnc_1.0.0_all.deb}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="artifacts/hardware/$stamp"
mkdir -p "$out"
phases="$out/phases.tsv"
: >"$phases"

record() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >>"$phases"; }
pass() {
  record "$1" passed "$2"
  echo "  ok: $2"
}
blocked() {
  record "$1" blocked "$2"
  echo "  BLOCKED: $2" >&2
}
fail() {
  record "$1" failed "$2"
  echo "  FAIL: $2" >&2
  finish
  exit 1
}
finish() {
  python3 - "$out" "$stamp" "$viewer_banner" <<'PY'
import sys
from pathlib import Path
from wayland_vnc.hardware import Report

out, stamp, viewer = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
import os, subprocess
report = Report(
    host=os.uname().nodename,
    session=os.environ.get("XDG_SESSION_TYPE", "unknown"),
    desktop=os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
    viewer=viewer,
    commit=subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=False).stdout.strip() or "unknown",
    recorded_at=stamp,
)
for line in (out / "phases.tsv").read_text().splitlines():
    phase, status, detail = line.split("\t", 2)
    report.record(phase, status, detail)
path = report.write(out / "report.json")
print(f"report: {path}  result={report.as_dict()['result']}")
PY
}

echo "=== preconditions ==="
[ "${XDG_SESSION_TYPE:-}" = wayland ] || {
  echo "needs a live Wayland session" >&2
  exit 2
}
[ ! -f /.dockerenv ] || {
  echo "refusing to run inside a container" >&2
  exit 2
}
sudo -n true 2>/dev/null || {
  echo "needs passwordless sudo for apt" >&2
  exit 2
}
command -v vncviewer >/dev/null || {
  echo "RealVNC Viewer (vncviewer) is required" >&2
  exit 2
}
viewer_banner=$(vncviewer --help 2>&1 || true)
viewer_banner=${viewer_banner%%$'\n'*}
case "$viewer_banner" in
*RealVNC*) ;;
*)
  echo "vncviewer is not the RealVNC Viewer: $viewer_banner" >&2
  exit 2
  ;;
esac
command -v Xvfb >/dev/null || {
  echo "Xvfb is required to host the viewer headlessly" >&2
  exit 2
}
[ -f "$deb" ] || {
  echo "built package missing: $deb (run the deb smoke first)" >&2
  exit 2
}
deb=$(readlink -f "$deb")
echo "  host=$(uname -n) desktop=${XDG_CURRENT_DESKTOP:-?} viewer=$viewer_banner"

ustate() { systemctl --user "$1" wayland-vnc.service 2>/dev/null || true; }
uprop() { systemctl --user show wayland-vnc.service -p "$1" --value 2>/dev/null || true; }
wait_active() {
  for _ in $(seq 1 25); do
    [ "$(ustate is-active)" = active ] && return 0
    sleep 1
  done
  return 1
}
# "active" alone is not enough: a unit whose serve fails after its checks is active
# for as long as those checks take (70 s on GNOME), and that window once hid a unit
# restarting every 75 s for hours. Settled means serve returned successfully (GNOME:
# RemainAfterExit leaves the unit active/exited) or is still running after that long
# (WayVNC: serve execs the server), with no automatic restart counted either way.
wait_settled() {
  for _ in $(seq 1 110); do
    [ "$(ustate is-active)" = active ] || return 1
    [ "$(uprop NRestarts)" = 0 ] || return 1
    [ "$(uprop SubState)" = exited ] && return 0
    sleep 1
  done
  [ "$(ustate is-active)" = active ] && [ "$(uprop NRestarts)" = 0 ]
}

echo "=== install ==="
sudo -n DEBIAN_FRONTEND=noninteractive apt-get purge -y wayland-vnc >/dev/null 2>&1 || true
systemctl --user reset-failed wayland-vnc.service 2>/dev/null || true
# The run validates the DEFAULT bind, so a bind stored by an earlier run on this
# machine is removed first; the credential is kept (the password phase replaces it).
rm -f -- "${XDG_CONFIG_HOME:-$HOME/.config}/wayland-vnc/wayvnc.conf"
sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y "$deb" 2>&1 |
  tee "$out/apt-install.log" >/dev/null || fail install "apt-get install failed"
if ! { command -v wayland-vnc >/dev/null && command -v wayland-vnc-settings >/dev/null; }; then
  fail install "launchers not on PATH after install"
fi
[ "$(systemctl --global is-enabled wayland-vnc.service)" = enabled ] || fail install "not globally enabled"
[ "$(ustate is-enabled)" = enabled ] || fail install "not enabled for this user"
wait_active || fail install "unit did not become active: $(ustate is-active)"
wait_settled || fail install "unit did not settle: $(ustate is-active)/$(uprop SubState), restarts=$(uprop NRestarts); see: journalctl --user -u wayland-vnc"
pass install "installed; unit enabled, ACTIVE and settled for this user with no manual step"

echo "=== app drawer ==="
if python3 - >"$out/drawer.log" 2>&1 <<'PY'; then
import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gio, Gtk

ID = "io.github.ventura8.wayland_vnc.Settings.desktop"
assert any(a.get_id() == ID for a in Gio.AppInfo.get_all()), "not in the app drawer"
theme = Gtk.IconTheme.get_default()
for name in ("io.github.ventura8.wayland_vnc", "io.github.ventura8.wayland_vnc-symbolic"):
    info = theme.lookup_icon(name, 64, 0)
    assert info and info.get_filename().startswith("/usr/share/icons/hicolor"), name
PY
  pass drawer "entry in the app drawer; both icons resolve from /usr/share/icons/hicolor"
else
  fail drawer "desktop entry or icon not visible to the shell (see $out/drawer.log)"
fi

# Where port 5900 is served: "loopback" when only 127.0.0.1/[::1] listen, "any" when
# a wildcard listener is present, "none" when nothing is, "other" otherwise.
listener_scope() {
  local listeners
  listeners=$(ss -tln 2>/dev/null | awk '$4 ~ /:5900$/ { print $4 }')
  [ -n "$listeners" ] || {
    echo none
    return
  }
  if printf '%s\n' "$listeners" | grep -qE '^(0\.0\.0\.0|\*|\[::\]):5900$'; then
    echo any
  elif printf '%s\n' "$listeners" | grep -qvE '^(127\.0\.0\.1|\[::1\]):5900$'; then
    echo other
  else
    echo loopback
  fi
}
await_scope() { # scope -> 0 once seen within 20s
  for _ in $(seq 1 40); do
    [ "$(listener_scope)" = "$1" ] && return 0
    sleep 0.5
  done
  return 1
}
set_lan_access() { # true|false, through the product's own action layer
  python3 - "$1" <<'PY'
import sys

from wayland_vnc.settings import Actions

Actions().set_lan_access(sys.argv[1] == "true")
PY
}

echo "=== service ==="
ss -tln 2>/dev/null | grep -qE ':5900\b' || fail service "nothing is listening on 5900"
# The default is this machine only: a user unit cannot fence a wildcard bind, so a
# fresh install that listened on every interface would be the product's central
# network promise broken.
[ "$(listener_scope)" = loopback ] ||
  fail service "port 5900 is not loopback-only by default (listeners: $(ss -tln | awk '$4 ~ /:5900$/ {print $4}' | tr '\n' ' '))"
# Identify the backend from the diagnostic, not from our unit's main process: on a
# GNOME host the unit configures gnome-remote-desktop and exits (RemainAfterExit), so
# there is no long-running process of ours to inspect.
doctor_json=$(wayland-vnc doctor --json 2>/dev/null || true)
backend=$(printf '%s' "$doctor_json" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["backend_candidate"] or "")' 2>/dev/null || true)
case "$backend" in
grd | wayvnc | w0vncserver) ;;
*) fail service "no supported backend was selected (got '$backend')" ;;
esac
serving=$(ss -tlnp 2>/dev/null | grep -E ':5900\b' | head -1)
echo "  serving: $serving"
pass service "port 5900 served on loopback only by default; backend=$backend"

echo "=== password (set through the product) ==="
testpw=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9')
testpw=${testpw:0:8}
printf '%s\n%s\n' "$testpw" "$testpw" |
  wayland-vnc set-password --username vnc --stdin >"$out/set-password.log" ||
  fail password "set-password failed"
printf 'username=vnc\npassword=%s\n' "$testpw" >"$out/test-credential"
chmod 600 "$out/test-credential"
pass password "viewer password set via 'wayland-vnc set-password'"

# The viewer uses the password we just set. On a GNOME host that password only takes
# effect once gnome-remote-desktop has reloaded it, which `set-password` now handles by
# restarting the daemon; give it a moment to come back before connecting.
viewer_blocked=""
viewerpw="$testpw"
if [ "$backend" = grd ]; then
  for _ in $(seq 1 15); do
    ss -tln 2>/dev/null | grep -qE ':5900\b' && break
    sleep 1
  done
  sleep 2
fi

connection_file() { # host password name -> path
  local file="$out/$3.vnc"
  python3 - "$1" "$2" >"$file" <<'PY'
import sys

from wayland_vnc.hardware import connection_file

print(connection_file(sys.argv[1], sys.argv[2]), end="")
PY
  chmod 600 "$file"
  echo "$file"
}

# Drive the REAL RealVNC Viewer on a private X server. $1=.vnc $2=png $3=log
# Returns 0 on a captured frame, 2 when the private X server never came up (a
# prerequisite of THIS host, not a product failure), 1 for anything else.
connect_and_capture() {
  local display=":1$((RANDOM % 80 + 20))"
  Xvfb "$display" -screen 0 1600x1000x24 >/dev/null 2>&1 &
  local xvfb=$!
  # Wait for Xvfb to own the display instead of assuming two seconds was enough. If
  # it died -- display number taken, Xvfb missing, no /tmp/.X11-unix -- the viewer
  # would fail for a reason that says nothing about the product.
  local socket="/tmp/.X11-unix/X${display#:}"
  local ready=1
  local _wait
  for _wait in $(seq 1 40); do
    kill -0 "$xvfb" 2>/dev/null || break
    if [ -S "$socket" ]; then
      ready=0
      break
    fi
    sleep 0.25
  done
  if [ "$ready" -ne 0 ]; then
    kill "$xvfb" 2>/dev/null || true
    wait "$xvfb" 2>/dev/null || true
    return 2
  fi
  DISPLAY="$display" vncviewer -AutoReconnect=0 -EnableUdpRfb=False -ProxyTcpRfb=0 \
    -PasswordStoreOffer=0 -Log='*:stderr:30' -VerifyId=0 -WarnUnencrypted=0 \
    -config "$1" >"$3" 2>&1 &
  local viewer=$!
  local status=1
  for _ in $(seq 1 40); do
    grep -q "Authentication successful" "$3" 2>/dev/null && {
      status=0
      break
    }
    kill -0 "$viewer" 2>/dev/null || break
    sleep 0.5
  done
  if [ $status -eq 0 ]; then
    sleep 3
    # The first streaming connection is when the data loop can be observed.
    if [ "$backend" = grd ] && [ ! -e "$out/stream-thread.txt" ]; then
      probe_stream_thread >"$out/stream-thread.txt"
    fi
    DISPLAY="$display" vncviewer -screenshot "$viewer" "$2" >/dev/null 2>&1 || status=1
  fi
  kill "$viewer" 2>/dev/null || true
  kill "$xvfb" 2>/dev/null || true
  wait "$viewer" 2>/dev/null || true
  return $status
}

# The daemon receives the screen on PipeWire's data loop thread ("data-loop.0"). Read
# its scheduling while a viewer is streaming: policy 0 is SCHED_OTHER, and the
# realtime CPU budget must be unlimited, i.e. libpipewire's module-rt was not loaded.
# Prints one line, or "no-data-loop" when the thread never appeared.
probe_stream_thread() {
  local pid task
  pid=$(systemctl --user show gnome-remote-desktop.service -p MainPID --value 2>/dev/null || echo 0)
  [ "${pid:-0}" -gt 0 ] || {
    echo "no-daemon"
    return
  }
  for _ in $(seq 1 20); do
    for task in /proc/"$pid"/task/*; do
      [ "$(cat "$task/comm" 2>/dev/null)" = "data-loop.0" ] || continue
      printf 'tid=%s policy=%s rttime=%s\n' "${task##*/}" \
        "$(awk '/^policy/ {print $3}' "$task/sched")" \
        "$(awk '/realtime timeout/ {print $4}' "$task/limits")"
      return
    done
    sleep 0.25
  done
  echo "no-data-loop"
}

frame_proof() {
  python3 - "$1" <<'PY'
import sys
from pathlib import Path

from wayland_vnc.hardware import frame_evidence

evidence = frame_evidence(Path(sys.argv[1]))
print(f"  frame {evidence.width}x{evidence.height}, {evidence.colours} distinct colours")
PY
}

lan=$(
  python3 - <<'PY'
from wayland_vnc import settings

info = settings.connect_info(settings.Actions().status().config)
print(info.addresses[0][1] if info.addresses else "")
PY
)

if [ -n "$viewer_blocked" ]; then
  blocked e2e-loopback "$viewer_blocked"
  blocked e2e-lan "$viewer_blocked"
  blocked bad-password "$viewer_blocked"
else
  echo "=== e2e: real RealVNC Viewer over loopback ==="
  conn=$(connection_file "127.0.0.1:5900" "$viewerpw" loopback)
  rc=0
  connect_and_capture "$conn" "$out/loopback.png" "$out/viewer-loopback.log" || rc=$?
  if [ "$rc" -eq 2 ]; then
    blocked e2e-loopback "no private X server on this host (Xvfb did not start)"
  elif [ "$rc" -ne 0 ]; then
    fail e2e-loopback "viewer did not authenticate over loopback"
  else
    frame_proof "$out/loopback.png" || fail e2e-loopback "loopback frame is not a real desktop"
    pass e2e-loopback "RealVNC authenticated over 127.0.0.1:5900 and received this desktop"
    if [ "$backend" = grd ]; then
      probe=$(cat "$out/stream-thread.txt" 2>/dev/null || echo "not-probed")
      case "$probe" in
      tid=*" policy=0 rttime=unlimited")
        pass stream-thread "daemon data loop is an ordinary thread with no realtime budget ($probe)"
        ;;
      *)
        fail stream-thread "daemon data loop is not a plain thread ($probe); a realtime loop is killed at 4K"
        ;;
      esac
    fi
  fi

  echo "=== local network access: opt in through the product ==="
  set_lan_access true >"$out/lan-on.log" 2>&1 || fail lan-on "set_lan_access(True) failed; see $out/lan-on.log"
  await_scope any || fail lan-on "listener did not move to every interface: $(listener_scope)"
  pass lan-on "local network access turned on; port 5900 now listens on every interface"

  echo "=== e2e: real RealVNC Viewer over the LAN address ==="
  if [ -z "$lan" ]; then
    blocked e2e-lan "no local-network address on this host"
  else
    echo "  LAN address shown in the app: $lan:5900"
    conn=$(connection_file "$lan:5900" "$viewerpw" lan)
    rc=0
    connect_and_capture "$conn" "$out/lan.png" "$out/viewer-lan.log" || rc=$?
    if [ "$rc" -eq 2 ]; then
      blocked e2e-lan "no private X server on this host (Xvfb did not start)"
    elif [ "$rc" -ne 0 ]; then
      fail e2e-lan "viewer did not authenticate over $lan:5900"
    else
      frame_proof "$out/lan.png" || fail e2e-lan "LAN frame is not a real desktop"
      pass e2e-lan "RealVNC authenticated over $lan:5900 (what a phone would use)"
    fi
  fi

  echo "=== local network access: opt out again, and the LAN address must refuse ==="
  set_lan_access false >"$out/lan-off.log" 2>&1 || fail lan-off "set_lan_access(False) failed; see $out/lan-off.log"
  await_scope loopback || fail lan-off "listener did not return to loopback: $(listener_scope)"
  if [ -z "$lan" ]; then
    pass lan-off "local network access turned off; port 5900 back on loopback only"
  else
    conn=$(connection_file "$lan:5900" "$viewerpw" lan-refused)
    rc=0
    connect_and_capture "$conn" "$out/lan-refused.png" "$out/viewer-lan-refused.log" || rc=$?
    if [ "$rc" -eq 0 ]; then
      fail lan-off "the LAN address still served a frame after opting out"
    elif [ "$rc" -eq 2 ]; then
      blocked lan-off "no private X server on this host (Xvfb did not start)"
    else
      pass lan-off "local network access turned off; $lan:5900 no longer answers"
    fi
  fi

  echo "=== bad: wrong password is refused by the real server ==="
  conn=$(connection_file "127.0.0.1:5900" "definitely-wrong" bad)
  rc=0
  connect_and_capture "$conn" "$out/bad.png" "$out/viewer-bad.log" || rc=$?
  if [ "$rc" -eq 0 ]; then
    fail bad-password "the server accepted a wrong password"
  elif [ "$rc" -eq 2 ]; then
    blocked bad-password "no private X server on this host (Xvfb did not start)"
  elif grep -qiE "authentication (failed|error)|auth.*fail|password check failed" "$out/viewer-bad.log"; then
    pass bad-password "wrong password refused; no frame delivered"
  else
    # Without the refusal in the log this proves only that the viewer did not get a
    # frame, which a stopped server or a wrong port would produce just as well.
    fail bad-password "no authentication failure in the viewer log; see $out/viewer-bad.log"
  fi
fi

echo "=== uninstall ==="
grd_before=$(systemctl --user is-active gnome-remote-desktop.service 2>/dev/null || echo inactive)
sudo -n DEBIAN_FRONTEND=noninteractive apt-get purge -y wayland-vnc 2>&1 |
  tee "$out/apt-purge.log" >/dev/null || fail uninstall "apt-get purge failed"
[ "$(ustate is-active)" != active ] || fail uninstall "our unit still active after purge"
[ "$(systemctl --global is-enabled wayland-vnc.service 2>/dev/null || echo gone)" != enabled ] ||
  fail uninstall "still globally enabled after purge"
for leftover in /usr/bin/wayland-vnc /usr/bin/wayland-vnc-settings /usr/lib/wayland-vnc \
  /usr/share/applications/io.github.ventura8.wayland_vnc.Settings.desktop; do
  [ ! -e "$leftover" ] || fail uninstall "left behind: $leftover"
done
if [ "$grd_before" = active ]; then
  [ "$(systemctl --user is-active gnome-remote-desktop.service)" = active ] ||
    fail uninstall "purge stopped the desktop's own GNOME Remote Desktop"
  echo "  the desktop's own gnome-remote-desktop.service was left running (correct)"
fi
pass uninstall "purge stopped and disabled our unit and left nothing behind"

echo "=== reinstall (leave the machine working) ==="
sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y "$deb" 2>&1 |
  tee "$out/apt-reinstall.log" >/dev/null || fail reinstall "reinstall failed"
wait_active || fail reinstall "unit not active after reinstall"
wait_settled || fail reinstall "unit did not settle after reinstall: restarts=$(uprop NRestarts)"
pass reinstall "reinstalled; unit active and settled again"

echo
finish
echo "Viewer password for this machine is now the test one in $out/test-credential"
[ -n "$lan" ] && echo "Connect a phone with RealVNC Viewer to $lan:5900"
echo "Change it any time in the Wayland VNC settings app."
