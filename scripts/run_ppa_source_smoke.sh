#!/usr/bin/env bash
# Build the PPA source package exactly the way .github/workflows/release.yml does,
# in a throwaway container, and check what Launchpad checks. Signing is the only
# difference: the workflow signs with the maintainer's key, this builds unsigned,
# so it can run without any credential.
#
# What it proves before a tag is cut: the generated changelog parses, the native
# source package builds, the .changes names the expected source, version, series
# and files, and lintian finds nothing at error severity.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH

version=$(tr -d '[:space:]' <VERSION)
# Matches PPA_UPLOAD_REVISION and the distro matrix in the release workflow.
revision=${PPA_UPLOAD_REVISION:-1}
series=${PPA_SERIES:-resolute}
maintainer_name=${MAINTAINER_NAME:-ventura8}
maintainer_email=${MAINTAINER_EMAIL:-alexandrescu.sergiu@gmail.com}
ppa_version="${version}+1ppa${revision}~${series}1"
out=artifacts/ppa
mkdir -p "$out"

runner=$(mktemp)
trap 'rm -f "$runner"' EXIT
cat >"$runner" <<'INNER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# Quiet on success; on failure show apt's own output, refresh the index and try once
# more. Ubuntu's archive is briefly inconsistent at times (an index naming a package
# version the pool answers 404 for), which the discarded output used to hide.
quiet_apt() {
  "$@" >/tmp/apt.log 2>&1 && return
  cat /tmp/apt.log >&2
  sleep 30
  apt-get update >/dev/null && "$@"
}
quiet_apt apt-get update
quiet_apt apt-get install -y --no-install-recommends \
  build-essential debhelper dpkg-dev devscripts lintian python3
mkdir -p /build && cp -a /src/. /build/ && cd /build
rm -rf debian/wayland-vnc debian/.debhelper debian/files ../wayland-vnc_* 2>/dev/null || true

echo "== the changelog the release workflow generates =="
cat >debian/changelog <<CHANGELOG
wayland-vnc (${PPA_VERSION}) ${SERIES}; urgency=medium

  * Release ${VERSION}
  * Not release-qualified: no qualification evidence for this commit
  * See https://github.com/ventura8/wayland-vnc/releases/tag/v${VERSION}

 -- ${MAINTAINER_NAME} <${MAINTAINER_EMAIL}>  $(date -R)
CHANGELOG
dpkg-parsechangelog >/dev/null
echo "  ok: parses as $(dpkg-parsechangelog -S Source) $(dpkg-parsechangelog -S Version) -> $(dpkg-parsechangelog -S Distribution)"

echo "== the version must sort above any earlier upload and below the next release =="
dpkg --compare-versions "${PPA_VERSION}" gt "${VERSION}" \
  || { echo "PPA version does not sort above the plain version" >&2; exit 1; }
dpkg --compare-versions "${PPA_VERSION}" lt "$(echo "${VERSION}" | awk -F. '{printf "%d.%d.%d", $1, $2, $3+1}')" \
  || { echo "PPA version does not sort below the next patch release" >&2; exit 1; }
echo "  ok: ${PPA_VERSION} sorts between ${VERSION} and the next patch release"

echo "== build the source package (unsigned; the workflow signs the same artefacts) =="
dpkg-checkbuilddeps
dpkg-buildpackage -S -sa -us -uc >/dev/null
changes=$(ls ../wayland-vnc_*_source.changes)
cp ../wayland-vnc_* /out/

echo "== what Launchpad reads from the upload =="
for field in Source Version Distribution Architecture; do
  printf '  %s: %s\n' "$field" "$(grep -m1 "^${field}: " "$changes" | cut -d' ' -f2-)"
done
grep -q "^Distribution: ${SERIES}$" "$changes" || { echo "wrong series in .changes" >&2; exit 1; }
grep -q "^Version: ${PPA_VERSION}$" "$changes" || { echo "wrong version in .changes" >&2; exit 1; }
grep -q "^Architecture: source$" "$changes" || { echo ".changes is not a source upload" >&2; exit 1; }
for suffix in .dsc .tar.xz; do
  ls ../wayland-vnc_*"$suffix" >/dev/null || { echo "missing $suffix in the upload" >&2; exit 1; }
done
echo "  ok: source-only upload with a .dsc and a native tarball"

echo "== lintian (errors fail; Launchpad builds anyway, we do not) =="
set +e
lintian --no-cfg --fail-on error --suppress-tags bad-distribution-in-changes-file "$changes"
lintian_status=$?
set -e
[ "$lintian_status" -eq 0 ] || { echo "lintian reported an error" >&2; exit 1; }
echo "  ok: no lintian errors"
echo "PPA SOURCE SMOKE PASSED"
INNER

echo "=== ppa source smoke: ubuntu:26.04 (${ppa_version}) ==="
docker run --rm --network bridge \
  -e VERSION="$version" -e PPA_VERSION="$ppa_version" -e SERIES="$series" \
  -e MAINTAINER_NAME="$maintainer_name" -e MAINTAINER_EMAIL="$maintainer_email" \
  -v "$PWD:/src:ro" -v "$runner:/runner.sh:ro" -v "$PWD/$out:/out" \
  "ubuntu:26.04@sha256:da6fc2be547864451aa253836dd926da33623312df4a9a243e35dc877c378a78" \
  bash /runner.sh
