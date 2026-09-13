#!/usr/bin/env bash
# Install the actual RealVNC Viewer for Android into the ISOLATED lab AVD from an APK
# file or URL -- a third-party mirror included -- but only after proving it is
# RealVNC's own build.
#
# RealVNC publishes no first-party APK download, and a Play Store install needs a
# Google account in the emulator. The maintainer decided (2026-09-17) that a mirror is
# an acceptable source, under one condition that no mirror can fake: the APK must
# carry RealVNC's own release signature. An Android APK's signature covers the whole
# file; a mirror that altered a byte could not re-sign it with RealVNC's key. So this
# script:
#   1. takes the APK from a local file or downloads it from the given URL;
#   2. verifies it with apksigner: signature scheme v2 or later must verify, and the
#      signer's certificate SHA-256 must equal the pinned RealVNC fingerprint below;
#   3. records package version, signer, ABI and file hash in the lab's provenance file;
#   4. installs it into the isolated AVD (never into a personal Android configuration).
# Nothing here signs into any account, and the APK is never committed.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH

usage() {
  echo "usage: $0 (--apk FILE | --url URL) [--sdk DIR] [--serial emulator-5554]" >&2
  exit 2
}

# RealVNC Ltd's release signing certificate, as verified on 2026-09-14 against the
# maintainer's locally obtained copy of RealVNC Viewer 4.9.4.60176 and again on
# 2026-09-17 against the copy installed in the lab AVD. Any other signer is refused.
REALVNC_SIGNER_SHA256=66d81472a2cad46121b6db13870a761425a833c93ca128c8b46a64d80307b7d9
REALVNC_SIGNER_DN_PREFIX="CN=RealVNC Ltd,"
PACKAGE=com.realvnc.viewer.android

apk=""
url=""
sdk=${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}
serial=${ANDROID_EMULATOR_SERIAL:-emulator-5554}
while [ $# -gt 0 ]; do
  case "$1" in
  --apk)
    apk=$2
    shift 2
    ;;
  --url)
    url=$2
    shift 2
    ;;
  --sdk)
    sdk=$2
    shift 2
    ;;
  --serial)
    serial=$2
    shift 2
    ;;
  *) usage ;;
  esac
done
[ -n "$apk" ] || [ -n "$url" ] || usage
[ -z "$apk" ] || [ -z "$url" ] || usage

apksigner=$(find "$sdk/build-tools" -mindepth 2 -maxdepth 2 -name apksigner 2>/dev/null | sort -V | tail -1)
aapt=$(find "$sdk/build-tools" -mindepth 2 -maxdepth 2 -name aapt2 2>/dev/null | sort -V | tail -1)
[ -x "$apksigner" ] || {
  echo "apksigner not found under $sdk/build-tools; install the build tools" >&2
  exit 2
}

root="$PWD/artifacts/android"
mkdir -p "$root"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

if [ -n "$url" ]; then
  echo "== downloading the APK =="
  echo "  from: $url"
  curl -fsSL --max-time 600 -o "$work/viewer.apk" "$url"
  apk="$work/viewer.apk"
fi
[ -f "$apk" ] || {
  echo "$apk is not a file" >&2
  exit 1
}
sha256=$(sha256sum "$apk" | cut -c1-64)
echo "  sha256: $sha256 ($(stat -c %s "$apk") bytes)"

echo "== verifying RealVNC's signature (this is what makes any source acceptable) =="
verify=$("$apksigner" verify --verbose --print-certs "$apk" 2>&1) || {
  echo "$verify" | tail -3 >&2
  echo "REFUSED: the APK's signature does not verify" >&2
  exit 1
}
echo "$verify" | grep -qE "^Verified using v(2|3|3\.1|3\.2) scheme .*: true" || {
  echo "REFUSED: no APK Signature Scheme v2 or later verified (v1 alone is not enough)" >&2
  exit 1
}
signer_dn=$(echo "$verify" | grep -m1 -E "^V[0-9.]+ Signer: certificate DN: " | sed 's/^.*certificate DN: //')
signer_sha=$(echo "$verify" | grep -m1 -E "^V[0-9.]+ Signer: certificate SHA-256 digest: " | sed 's/^.*digest: //')
echo "  signer: $signer_dn"
echo "  signer certificate SHA-256: $signer_sha"
[ "$signer_sha" = "$REALVNC_SIGNER_SHA256" ] || {
  echo "REFUSED: signer certificate is not RealVNC's pinned release certificate" >&2
  exit 1
}
case "$signer_dn" in
"$REALVNC_SIGNER_DN_PREFIX"*) ;;
*)
  echo "REFUSED: signer DN does not start with '$REALVNC_SIGNER_DN_PREFIX'" >&2
  exit 1
  ;;
esac
signers=$(echo "$verify" | grep -cE "^V[0-9.]+ Signer: certificate SHA-256 digest: " || true)
[ "$signers" -eq 1 ] || {
  echo "REFUSED: expected exactly one signer, found $signers" >&2
  exit 1
}
echo "  ok: signed by RealVNC Ltd with the pinned certificate"

package=""
version=""
if [ -x "$aapt" ]; then
  badging=$("$aapt" dump badging "$apk" 2>/dev/null || true)
  package=$(echo "$badging" | sed -n "s/^package: name='\([^']*\)'.*/\1/p" | head -1)
  version=$(echo "$badging" | sed -n "s/^package: .*versionName='\([^']*\)'.*/\1/p" | head -1)
  [ -z "$package" ] || [ "$package" = "$PACKAGE" ] || {
    echo "REFUSED: the APK is $package, not $PACKAGE" >&2
    exit 1
  }
  echo "  package: ${package:-unknown} version: ${version:-unknown}"
fi

echo "== installing into the isolated AVD ($serial) =="
export ANDROID_SDK_ROOT="$sdk" ANDROID_AVD_HOME="$root/avd" ANDROID_USER_HOME="$root/user"
adb="$sdk/platform-tools/adb"
"$adb" -s "$serial" get-state >/dev/null 2>&1 || {
  echo "no device at $serial; boot the lab AVD first (scripts/android-stage.sh)" >&2
  exit 1
}
"$adb" -s "$serial" install -r "$apk" >/dev/null
installed=$("$adb" -s "$serial" shell dumpsys package "$PACKAGE" | tr -d '\r' | sed -n 's/^ *versionName=//p' | head -1)
abi=$("$adb" -s "$serial" shell dumpsys package "$PACKAGE" | tr -d '\r' | sed -n 's/^ *primaryCpuAbi=//p' | head -1)
echo "  installed: $PACKAGE $installed ($abi)"

provenance="$root/viewer-provenance.json"
python3 - "$provenance" "$sha256" "$signer_dn" "$signer_sha" "$installed" "$abi" "${url:-local file}" <<'PY'
import json, sys
from datetime import UTC, datetime
path, sha, dn, signer, version, abi, source = sys.argv[1:]
record = {
    "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "package": "com.realvnc.viewer.android",
    "version": version,
    "abi": abi,
    "apk_sha256": sha,
    "apk_source": source,
    "signature_scheme": "APK Signature Scheme v2 or later (apksigner verify)",
    "signer_dn": dn,
    "signer_cert_sha256": signer,
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2)
    handle.write("\n")
print(f"  provenance: {path}")
PY
echo "ok: RealVNC Viewer for Android is installed in the lab AVD; no account was used"
