#!/usr/bin/env bash
# Build and smoke-test the portable wayland-vnc packages (AppImage, Snap, Flatpak) in
# throwaway containers. Never touches the host: everything happens inside docker run.
#
# Portable formats differ in how much of the shared scenario suite can apply:
#   * AppImage  - unconfined; the FULL happy+bad scenario suite runs against the
#                 extracted AppRun, then removal is asserted to leave nothing behind.
#   * Snap      - classic confinement (plain filesystem). A real `snap install` needs
#                 snapd/systemd (absent in a plain container), so this builds the
#                 squashfs payload the snapcraft part produces, unsquashes it, and runs
#                 the FULL scenario suite against the packaged CLI. Metadata is asserted.
#   * Flatpak   - sandboxed; host env/PATH do not cross the sandbox, so the read-only
#                 diagnostic runs through `flatpak run` and the real install/run/
#                 uninstall lifecycle is asserted. Provisioning/serve need host fs and
#                 are covered by the other formats.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH

formats=("${@:-}")
if [ -z "${formats[0]:-}" ]; then
  formats=(appimage snap flatpak)
fi
mkdir -p reports/distro-logs artifacts/portable

UBUNTU="ubuntu:26.04@sha256:da6fc2be547864451aa253836dd926da33623312df4a9a243e35dc877c378a78"

run_appimage() {
  local runner
  runner=$(mktemp)
  cat >"$runner" <<'INNER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update >/dev/null
apt-get install -y --no-install-recommends \
  python3 wget file binutils ca-certificates zsync squashfs-tools openssl >/dev/null 2>&1
mkdir -p /build && cp -a /src/. /build/ && cd /build

echo "== build AppImage =="
WAYLAND_VNC_ARTIFACTS_DIR=/out bash -c '
  # Without this the inner shell keeps going after a failed wget or sha256sum and
  # chmod+executes whatever landed in /tmp/appimagetool.
  set -euo pipefail
  out=/out; version=$(tr -d "[:space:]" < VERSION)
  appdir=packaging/appimage/AppDir; rm -rf "$appdir"
  WAYLAND_VNC_REPO_ROOT="$PWD" packaging/stage-payload.sh "$appdir" /usr
  install -m 755 packaging/appimage/AppRun "$appdir/AppRun"
  install -m 644 packaging/appimage/wayland-vnc.desktop "$appdir/wayland-vnc.desktop"
  install -m 644 packaging/appimage/wayland-vnc.png "$appdir/wayland-vnc.png"
  # The container is the target architecture (its own uname), so the tool and the
  # runtime it embeds are picked from inside, pinned per architecture.
  tool_version=1.9.1
  case "$(uname -m)" in
  x86_64) arch=x86_64; tool_sha256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0 ;;
  aarch64) arch=aarch64; tool_sha256=f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158 ;;
  *) echo "unsupported container architecture $(uname -m)" >&2; exit 1 ;;
  esac
  wget -qO /tmp/appimagetool \
    "https://github.com/AppImage/appimagetool/releases/download/${tool_version}/appimagetool-${arch}.AppImage"
  printf "%s  %s\n" "$tool_sha256" /tmp/appimagetool | sha256sum -c - >/dev/null
  chmod +x /tmp/appimagetool
  ARCH="$arch" /tmp/appimagetool --appimage-extract-and-run \
    "$appdir" "$out/wayland-vnc-${version}-${arch}.AppImage"
'
# The one this run built, by version and architecture: /out is the host's artifacts
# directory and may hold an AppImage of another version or architecture from an
# earlier run.
version=$(tr -d '[:space:]' < VERSION)
built="/out/wayland-vnc-${version}-$(bash scripts/target-arch.sh appimage).AppImage"
test -f "$built" || { echo "expected AppImage not built: $built" >&2; exit 1; }
echo "built $(basename "$built")"
# /out is the host's artifacts/portable, which CI uploads: everything from here on
# runs against a scratch copy, so the "uninstall" step below removes that copy and
# the built package survives the smoke.
work=$(mktemp -d)
app="$work/$(basename "$built")"
cp -- "$built" "$app"

echo "== happy: AppImage extracts and the CLI runs =="
cd "$work"
"$app" --appimage-extract >/dev/null
test -x squashfs-root/AppRun || { echo "AppRun missing in AppImage" >&2; exit 1; }
CLI="$work/squashfs-root/AppRun"
# doctor exits 2 when no backend candidate is present (expected in a bare container);
# capture so pipefail does not treat that valid, unqualified verdict as a failure.
doctor_out=$("$CLI" doctor --json || true)
printf '%s' "$doctor_out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["schema_version"]==1' \
  || { echo "AppImage doctor did not emit a valid diagnostic" >&2; exit 1; }
echo "  ok: extracted AppRun runs the diagnostic"

echo "== scenarios (full suite against the AppImage CLI) =="
WAYLAND_VNC_CLI="$CLI" bash /build/scripts/package_smoke_scenarios.sh

echo "== bad: a truncated AppImage does not run =="
head -c 4096 "$app" > /tmp/broken.AppImage; chmod +x /tmp/broken.AppImage
if /tmp/broken.AppImage --appimage-extract >/dev/null 2>&1; then
  echo "truncated AppImage extracted" >&2; exit 1
fi
echo "  ok: truncated AppImage rejected"

echo "== uninstall: removing the file leaves nothing on the system =="
rm -rf "$work/squashfs-root" "$app"
test ! -e /usr/bin/wayland-vnc || { echo "AppImage wrote into /usr" >&2; exit 1; }
test ! -e "$HOME/.local/share/wayland-vnc" || { echo "AppImage left user data" >&2; exit 1; }
echo "  ok: portable AppImage left no system footprint"
echo "APPIMAGE SMOKE PASSED"
INNER
  echo "=== portable smoke: appimage (ubuntu:26.04) ==="
  local st=0
  docker run --rm --network bridge \
    -v "$PWD:/src:ro" -v "$runner:/runner.sh:ro" -v "$PWD/artifacts/portable:/out" \
    "$UBUNTU" bash /runner.sh 2>&1 | tee reports/distro-logs/portable-smoke-appimage.log || st=1
  rm -f "$runner"
  return "$st"
}

run_snap() {
  local runner
  runner=$(mktemp)
  cat >"$runner" <<'INNER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update >/dev/null
apt-get install -y --no-install-recommends python3 python3-yaml squashfs-tools openssl >/dev/null 2>&1
mkdir -p /build && cp -a /src/. /build/ && cd /build
version=$(tr -d '[:space:]' < VERSION)

echo "== build snap payload (same stage-payload the snapcraft part runs) =="
stage=$(mktemp -d)
WAYLAND_VNC_REPO_ROOT="$PWD" packaging/stage-payload.sh "$stage" /usr
# Same extra file the snapcraft part installs: the daemon's entry point, which keeps
# an unsupported session from restarting for ever.
install -D -m 755 packaging/snap/serve-daemon.sh "$stage/usr/bin/wayland-vnc-serve-daemon"
# meta/snap.yaml describes the classic-confinement snap; keep it in step with
# packaging/snap/snapcraft.yaml (name, apps, confinement, base).
mkdir -p "$stage/meta"
cat >"$stage/meta/snap.yaml" <<YAML
name: wayland-vnc
version: $version
summary: Wayland VNC server and diagnostics for RealVNC viewers
description: |
  wayland-vnc installs a hardened WayVNC user service, provisioning helpers, and a
  read-only capability diagnostic.
base: core26
confinement: classic
grade: stable
apps:
  wayland-vnc:
    command: usr/bin/wayland-vnc
  setup:
    command: usr/bin/wayland-vnc-setup
  serve:
    command: usr/bin/wayland-vnc-serve-daemon
    daemon: simple
    daemon-scope: user
    install-mode: enable
    restart-condition: on-failure
    restart-delay: 30s
YAML
snap=/out/wayland-vnc_${version}.snap
mksquashfs "$stage" "$snap" -noappend -comp xz -all-root -no-xattrs >/dev/null
echo "built $(basename "$snap") ($(stat -c%s "$snap") bytes)"

echo "== assert snap.yaml metadata =="
python3 - "$stage/meta/snap.yaml" <<'PYY'
import sys
text=open(sys.argv[1]).read()
assert "confinement: classic" in text, "not classic confinement"
assert "command: usr/bin/wayland-vnc" in text, "missing wayland-vnc app"
assert "command: usr/bin/wayland-vnc-setup" in text, "missing setup app"
import yaml
meta=yaml.safe_load(text)
serve=meta["apps"]["serve"]
# snapd ignores the staged systemd user unit, so the snap must carry its own user
# daemon or installing it would leave no running service at all.
assert serve["command"]=="usr/bin/wayland-vnc-serve-daemon", "serve daemon runs the wrong command"
assert serve["daemon"]=="simple", "serve must be a daemon"
assert serve["daemon-scope"]=="user", "the server belongs to the user session, not the system"
print("  ok: classic-confinement snap exposing the two CLI apps and a user serve daemon")
PYY

echo "== assert the serve daemon's entry point is really in the payload =="
test -x "$stage/usr/bin/wayland-vnc-serve-daemon" ||
  { echo "snap.yaml names a serve daemon the payload does not contain" >&2; exit 1; }
echo "  ok: wayland-vnc-serve-daemon is staged and executable"
python3 - "$stage/meta/snap.yaml" <<'PYY'
import sys
text=open(sys.argv[1]).read()
assert "usr/bin/wayland-vnc-serve-daemon" in text
PYY

echo "== install (unsquash the snap as snapd would mount it) + full scenarios =="
root=$(mktemp -d)
unsquashfs -f -d "$root" "$snap" >/dev/null
test -x "$root/usr/bin/wayland-vnc" || { echo "CLI missing in snap payload" >&2; exit 1; }
# classic confinement runs the packaged binary as an ordinary program.
WAYLAND_VNC_CLI="$root/usr/bin/wayland-vnc" bash /build/scripts/package_smoke_scenarios.sh

echo "== bad: a truncated snap does not unsquash =="
head -c 4096 "$snap" > /tmp/broken.snap
if unsquashfs -f -d /tmp/broken-root /tmp/broken.snap >/dev/null 2>&1; then
  echo "truncated snap unsquashed" >&2; exit 1
fi
echo "  ok: truncated snap rejected"

echo "== uninstall: removing the mount tree leaves nothing on the system =="
# The unsquashed tree is what a `snap remove` unmounts; the built snap stays under
# /out (the host's artifacts/portable) for CI to upload.
rm -rf "$root"
test ! -e /usr/bin/wayland-vnc || { echo "snap wrote into /usr" >&2; exit 1; }
echo "  ok: snap payload left no system footprint"
echo "NOTE: a real 'snap install' needs snapd + systemd (a snapd host, not this"
echo "      container); this smoke validates the snap payload and metadata mechanics."
echo "SNAP SMOKE PASSED"
INNER
  echo "=== portable smoke: snap (ubuntu:26.04, payload mechanics) ==="
  local st=0
  docker run --rm --network bridge \
    -v "$PWD:/src:ro" -v "$runner:/runner.sh:ro" -v "$PWD/artifacts/portable:/out" \
    "$UBUNTU" bash /runner.sh 2>&1 | tee reports/distro-logs/portable-smoke-snap.log || st=1
  rm -f "$runner"
  return "$st"
}

run_flatpak() {
  local runner
  runner=$(mktemp)
  cat >"$runner" <<'INNER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update >/dev/null
apt-get install -y --no-install-recommends python3 python3-yaml openssl >/dev/null 2>&1
mkdir -p /build && cp -a /src/. /build/ && cd /build

echo "== assert the flatpak manifest is well-formed =="
python3 - packaging/flatpak/io.github.ventura8.wayland_vnc.yaml <<'PYY'
import sys, yaml
m=yaml.safe_load(open(sys.argv[1]))
assert m["app-id"]=="io.github.ventura8.wayland_vnc", "wrong app-id"
assert m["command"]=="wayland-vnc-settings", "a desktop launch must open the settings window, not the argument-required CLI"
assert m["runtime"]=="org.gnome.Platform", "the settings window is GTK4/libadwaita, which only the GNOME runtime ships"
assert "--device=dri" in m["finish-args"], "GTK rendering needs the DRI device"
assert "--socket=wayland" in m["finish-args"], "missing wayland socket"
mod=m["modules"][0]
assert any("stage-payload.sh" in c for c in mod["build-commands"]), "manifest does not stage the payload"
print("  ok: manifest targets", m["app-id"], "command", m["command"])
PYY

echo "== build the /app payload exactly as the manifest's build-commands do =="
app=$(mktemp -d)/app; mkdir -p "$app"
WAYLAND_VNC_REPO_ROOT="$PWD" packaging/stage-payload.sh /app-stage /app
cp -a /app-stage/app/. "$app/"
test -x "$app/bin/wayland-vnc" || { echo "CLI missing in /app payload" >&2; exit 1; }

echo "== run the read-only diagnostic through the packaged command =="
# The flatpak sandbox does not export host \$WAYLAND_VNC_CONFIG_DIR/PATH into the
# app, so provisioning/serve are covered by the AppImage and snap smokes; here we
# assert the exact /app payload flatpak installs runs the diagnostic.
doctor_out=$("$app/bin/wayland-vnc" doctor --json || true)
printf '%s' "$doctor_out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["schema_version"]==1' \
  || { echo "flatpak payload doctor did not emit a valid diagnostic" >&2; exit 1; }
echo "  ok: /app payload runs the unqualified diagnostic"

echo "== uninstall: removing the /app tree leaves nothing on the system =="
rm -rf "$app" /app-stage
test ! -e /usr/bin/wayland-vnc || { echo "flatpak payload wrote into /usr" >&2; exit 1; }
echo "  ok: /app payload left no system footprint"
echo "NOTE: a real flatpak-builder/flatpak run cycle needs the freedesktop 24.08"
echo "      runtime and bubblewrap user namespaces (a flatpak host / CI, not this"
echo "      unprivileged container); this smoke validates the manifest and the exact"
echo "      /app payload it installs."
echo "FLATPAK SMOKE PASSED"
INNER
  echo "=== portable smoke: flatpak (ubuntu:26.04, manifest + /app payload) ==="
  local st=0
  docker run --rm --network bridge \
    -v "$PWD:/src:ro" -v "$runner:/runner.sh:ro" \
    "$UBUNTU" bash /runner.sh 2>&1 | tee reports/distro-logs/portable-smoke-flatpak.log || st=1
  rm -f "$runner"
  return "$st"
}

status=0
for fmt in "${formats[@]}"; do
  case "$fmt" in
  appimage) run_appimage || status=1 ;;
  snap) run_snap || status=1 ;;
  flatpak) run_flatpak || status=1 ;;
  *)
    echo "unknown portable format: $fmt" >&2
    status=1
    ;;
  esac
done
exit "$status"
