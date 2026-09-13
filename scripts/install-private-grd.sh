#!/usr/bin/env bash
# Put the project's private GNOME Remote Desktop build in front of the distribution's
# on THIS host, for a GNOME desktop whose stock VNC daemon crashes.
#
# The distribution's VNC-enabled gnome-remote-desktop (50.2+vnc on Ubuntu 26.04)
# segfaults whenever a client disconnects: rfb_client is used after clientGone
# cleared it, right after rfbProcessEvents(). Every reconnect from a phone then kills
# the daemon again, and RealVNC Viewer sits in "attempting to reconnect". The build
# under docker/Dockerfile.gnome carries the client-lifetime guard
# (patches/gnome-remote-desktop/0001-client-lifetime.patch) and the private
# LibVNCServer, installed under /opt/wayland-vnc, which is where backends.py expects it.
#
# What this changes on the host, and only this:
#   /opt/wayland-vnc/grd and /opt/wayland-vnc/libvnc   (new; the distro package is untouched)
#   ~/.config/systemd/user/gnome-remote-desktop.service.d/20-wayland-vnc-private-daemon.conf
# Undo: remove the drop-in, `systemctl --user daemon-reload`, restart the unit.
#
# `--debug` adds a second drop-in that turns on the daemon's own debug log
# (G_MESSAGES_DEBUG=all: every connection decision, session start and stop, stream
# state). It is for diagnosing a viewer on a development host and is never part of a
# release install; `--no-debug` removes it again.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

image=${WAYLAND_VNC_GNOME_IMAGE:-wayland-vnc-gnome:dev}
dropin_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/gnome-remote-desktop.service.d"
dropin="$dropin_dir/20-wayland-vnc-private-daemon.conf"
debug_dropin="$dropin_dir/30-wayland-vnc-daemon-debug.conf"
debug=keep
case "${1:-}" in
"") ;;
--debug) debug=on ;;
--no-debug) debug=off ;;
*)
  echo "usage: $0 [--debug|--no-debug]" >&2
  exit 2
  ;;
esac

if ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "image $image is not built; run scripts/fixture-smoke.sh gnome first" >&2
  exit 2
fi
release() { sed -n 's/^VERSION_ID="\(.*\)"$/\1/p' "$1"; }
host_release=$(release /etc/os-release)
image_release=$(docker run --rm --entrypoint sh "$image" -c 'sed -n '"'"'s/^VERSION_ID="\(.*\)"$/\1/p'"'"' /etc/os-release')
if [ "$host_release" != "$image_release" ]; then
  echo "the private build was made on Ubuntu $image_release; this host is $host_release" >&2
  exit 2
fi

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
container=$(docker create "$image")
docker cp "$container:/opt/wayland-vnc" "$stage/wayland-vnc" >/dev/null
docker rm -f "$container" >/dev/null

echo "== installing /opt/wayland-vnc/{grd,libvnc} (sudo) =="
sudo install -d -m 755 /opt/wayland-vnc
# Stage beside the live tree and validate THAT, so a build which does not link on this
# host cannot leave the machine with the old daemon deleted and nothing serving. The
# swap happens only after the check passes, and the previous tree is kept until it has.
incoming=/opt/wayland-vnc/.incoming
previous=/opt/wayland-vnc/.previous
sudo rm -rf "$incoming" "$previous"
sudo install -d -m 755 "$incoming"
sudo cp -a "$stage/wayland-vnc/grd" "$stage/wayland-vnc/libvnc" "$incoming/"
sudo chown -R root:root "$incoming"
if ldd "$incoming/grd/libexec/gnome-remote-desktop-daemon" | grep -q "not found"; then
  echo "the private daemon does not link on this host; the installed one is untouched:" >&2
  ldd "$incoming/grd/libexec/gnome-remote-desktop-daemon" | grep "not found" >&2
  sudo rm -rf "$incoming"
  exit 1
fi
restore() {
  for part in grd libvnc; do
    if [ -d "$previous/$part" ]; then
      sudo rm -rf "/opt/wayland-vnc/$part"
      sudo mv "$previous/$part" "/opt/wayland-vnc/$part"
    fi
  done
  sudo rm -rf "$incoming" "$previous"
}
sudo install -d -m 755 "$previous"
for part in grd libvnc; do
  if [ -d "/opt/wayland-vnc/$part" ]; then
    sudo mv "/opt/wayland-vnc/$part" "$previous/$part" || {
      restore
      echo "could not set the previous $part aside; nothing was replaced" >&2
      exit 1
    }
  fi
  sudo mv "$incoming/$part" "/opt/wayland-vnc/$part" || {
    restore
    echo "could not install the new $part; the previous one was put back" >&2
    exit 1
  }
done
# The previous tree stays until the new daemon has been seen serving: a build that
# links but dies at start would otherwise leave the machine with no VNC server and
# nothing to go back to. The drop-in is restored with it, so a first install that
# fails does not leave a drop-in pointing at a daemon that was rolled away.
saved_dropin=
if [ -f "$dropin" ]; then
  saved_dropin=$(mktemp)
  cp -- "$dropin" "$saved_dropin"
fi
roll_back() {
  echo "rolling back to the previously installed daemon" >&2
  restore
  if [ -n "$saved_dropin" ]; then
    cp -- "$saved_dropin" "$dropin"
    rm -f -- "$saved_dropin"
  else
    rm -f -- "$dropin"
  fi
  systemctl --user daemon-reload
  systemctl --user reset-failed gnome-remote-desktop.service || true
  systemctl --user restart gnome-remote-desktop.service || true
}

echo "== pointing gnome-remote-desktop.service at it (user drop-in) =="
mkdir -p "$(dirname "$dropin")"
cat >"$dropin" <<'CONF'
# Installed by wayland-vnc (scripts/install-private-grd.sh). The distribution's
# VNC-enabled gnome-remote-desktop segfaults whenever a client disconnects
# (rfb_client is used after clientGone cleared it). This runs the project's private
# build, which carries the client-lifetime guard, from /opt/wayland-vnc. Remove this
# file and `systemctl --user daemon-reload` to go back to the distribution daemon.
[Service]
ExecStart=
ExecStart=/opt/wayland-vnc/grd/libexec/gnome-remote-desktop-daemon --vnc-port 5900
Environment=WAYLAND_VNC_ENABLE_DMABUF=0
Environment=WAYLAND_VNC_ENABLE_CLIPBOARD=0
CONF
case "$debug" in
on)
  cat >"$debug_dropin" <<'CONF'
# Development-only: the daemon's own debug log (connections, sessions, streams).
# Added by scripts/install-private-grd.sh --debug; remove with --no-debug.
[Service]
Environment=G_MESSAGES_DEBUG=all
CONF
  echo "== daemon debug logging ON ($debug_dropin) =="
  ;;
off)
  rm -f "$debug_dropin"
  echo "== daemon debug logging off =="
  ;;
esac
systemctl --user daemon-reload
systemctl --user reset-failed gnome-remote-desktop.service || true
if ! systemctl --user restart gnome-remote-desktop.service; then
  echo "the private daemon failed to start; see: journalctl --user -u gnome-remote-desktop" >&2
  roll_back
  exit 1
fi

for _ in $(seq 1 40); do
  if ss -ltn | grep -q ":5900 "; then
    echo "ok: private gnome-remote-desktop is serving VNC on port 5900"
    sudo rm -rf "$incoming" "$previous"
    rm -f -- "${saved_dropin:-}"
    exit 0
  fi
  sleep 0.25
done
echo "the private daemon started but nothing listens on 5900; see: journalctl --user -u gnome-remote-desktop" >&2
roll_back
exit 1
