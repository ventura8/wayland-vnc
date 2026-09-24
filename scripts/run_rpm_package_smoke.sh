#!/usr/bin/env bash
# Build the wayland-vnc RPM and exercise install / reinstall / erase and the happy+bad
# scenario suite in throwaway RPM-family containers (Fedora, Rocky, openSUSE). Never
# touches the host.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH

images=("${@:-}")
if [[ -z "${images[0]:-}" ]]; then
  images=(fedora:44 almalinux:10 opensuse/tumbleweed:latest)
fi
mkdir -p artifacts/rpm reports/distro-logs

runner=$(mktemp)
trap 'rm -f "$runner"' EXIT
cat >"$runner" <<'INNER'
set -euo pipefail
version=$(tr -d '[:space:]' < /src/VERSION)

# Build tooling only; runtime deps (wayvnc) vary by distro repo and are decoupled
# from package mechanics (the scenario suite shadows wayvnc with a fake on PATH).
# stdout is discarded but stderr is not: swallowing both turned a failed install into
# eight silent seconds and an unexplained "rpm smoke failed" in CI.
if command -v dnf >/dev/null 2>&1; then
  dnf install -y rpm-build python3 systemd openssl >/dev/null
else
  # A freshly pulled Tumbleweed carries repo metadata that may already be stale, and
  # installing without refreshing it first is what made this leg flaky.
  zypper --non-interactive --gpg-auto-import-keys refresh >/dev/null
  zypper --non-interactive install --no-recommends \
    rpm-build python3 systemd openssl >/dev/null
fi

top=/root/rpmbuild
mkdir -p "$top"/{BUILD,RPMS,SOURCES,SPECS,BUILDROOT}
cp -a /src "$top/SOURCES/repo"
cp -a /src /root/repo
cp /root/repo/packaging/rpm/wayland-vnc.spec "$top/SPECS/"

echo "== build =="
( cd /root/repo && rpmbuild --define "_topdir $top" --define "vnc_version $version" \
  -bb "$top/SPECS/wayland-vnc.spec" >/dev/null 2>&1 )
rpm=$(find "$top/RPMS" -name 'wayland-vnc-*.rpm' | head -1)
cp "$rpm" /out/ 2>/dev/null || true
echo "built $(basename "$rpm")"

echo "== bad: reject a truncated rpm =="
head -c 200 "$rpm" > /tmp/broken.rpm
if rpm -qp /tmp/broken.rpm >/dev/null 2>&1; then echo "rpm accepted a truncated file" >&2; exit 1; fi
echo "  ok: truncated rpm rejected"

echo "== happy: install (rpm --nodeps: package mechanics, not repo coverage) =="
rpm -Uvh --nodeps "$rpm" >/dev/null 2>&1
command -v wayland-vnc >/dev/null || { echo "CLI missing after install" >&2; exit 1; }
test -f /usr/lib/systemd/user/wayland-vnc.service || { echo "unit missing after install" >&2; exit 1; }

echo "== reinstall is idempotent =="
rpm -Uvh --replacepkgs --nodeps "$rpm" >/dev/null 2>&1

echo "== scenarios =="
WAYLAND_VNC_CLI=wayland-vnc bash /root/repo/scripts/package_smoke_scenarios.sh

echo "== erase removes everything =="
rpm -e wayland-vnc >/dev/null 2>&1
test ! -e /usr/bin/wayland-vnc || { echo "CLI left after erase" >&2; exit 1; }
test ! -d /usr/lib/wayland-vnc || { echo "module tree left after erase" >&2; exit 1; }
echo "  ok: erase left nothing behind"
echo "RPM SMOKE PASSED"
INNER

status=0
for image in "${images[@]}"; do
  slug=${image//[:\/]/-}
  echo "=== rpm smoke: $image ==="
  if ! docker run --rm --network bridge \
    -v "$PWD:/src:ro" -v "$runner:/runner.sh:ro" -v "$PWD/artifacts/rpm:/out" \
    "$image" bash /runner.sh \
    2>&1 | tee "reports/distro-logs/rpm-smoke-${slug}.log"; then
    status=1
    echo "rpm smoke failed on $image" >&2
  fi
done
exit "$status"
