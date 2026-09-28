#!/usr/bin/env bash
# Upload one signed source upload (.changes) to the Launchpad PPA over SFTP.
#
# Why SFTP: since 1.0.1 Launchpad's anonymous FTP accepted the first source upload of
# a release run and answered the second (wayland-vnc-grd) with "550 Requested action
# not taken: internal server error", every time -- the grd package stopped reaching
# the PPA and the GitHub release job, which waits on the upload, never ran. An
# authenticated SFTP upload is not subject to that. mixxx hit the same failure and
# moved to SFTP too (mixxxdj/mixxx#16983, #17107).
#
# Why the skip: a re-run of a half-failed release job used to upload the first
# package again, which Launchpad can only reject as a duplicate. A source version the
# PPA already records is reported and skipped, so re-running the job finishes the job.
#
# Environment:
#   PPA_NAME             ppa:OWNER/NAME; OWNER is also the Launchpad login
#   PPA_SSH_PRIVATE_KEY  the private key registered with that Launchpad account
#   LAUNCHPAD_API        API root (default https://api.launchpad.net/devel)
set -euo pipefail

changes=${1:?"usage: ppa-upload.sh SOURCE.changes"}
[[ -f "$changes" ]] || {
  echo "no such .changes file: $changes" >&2
  exit 2
}
# Resolved before the cd below, so a path relative to the caller still names the file.
changes=$(realpath -- "$changes")
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
ppa=${PPA_NAME:?PPA_NAME is required (ppa:OWNER/NAME)}
[[ "$ppa" =~ ^ppa:([a-z0-9][a-z0-9.+-]*)/([a-z0-9][a-z0-9.+-]*)$ ]] || {
  echo "PPA_NAME must look like ppa:OWNER/NAME, got '$ppa'" >&2
  exit 2
}
owner=${BASH_REMATCH[1]}
name=${BASH_REMATCH[2]}
api=${LAUNCHPAD_API:-https://api.launchpad.net/devel}
source=$(sed -n 's/^Source: *//p' "$changes" | head -1)
version=$(sed -n 's/^Version: *//p' "$changes" | head -1)
[[ -n "$source" && -n "$version" ]] || {
  echo "$changes names no Source and Version" >&2
  exit 2
}

# Every record of this exact source version, in any state: Launchpad refuses a version
# it has ever accepted, deleted ones included, so any record means "already uploaded".
records=$(curl -fsS --proto =https --connect-timeout 30 --max-time 120 -G \
  "$api/~$owner/+archive/ubuntu/$name" \
  --data-urlencode "ws.op=getPublishedSources" \
  --data-urlencode "source_name=$source" \
  --data-urlencode "version=$version" \
  --data-urlencode "exact_match=true" |
  jq -r '.entries[] | "\(.source_package_version) \(.status)"')
if [[ -n "$records" ]]; then
  echo "$source $version is already in $ppa ($(head -1 <<<"$records" | cut -d' ' -f2)); not uploading it again"
  exit 0
fi

key=${PPA_SSH_PRIVATE_KEY:-}
[[ -n "$key" ]] || {
  echo "PPA_SSH_PRIVATE_KEY is empty: add the upload key to the ppa-release environment" \
    "(docs/ppa-setup.md); anonymous FTP refuses the second upload of a run" >&2
  exit 1
}

# dput's SFTP method runs plain `ssh` and passes it no options for either, so the
# identity and the host key come from ~/.ssh/config (on the release runner, $HOME is
# that account's home): this key only, the pinned host key only, nothing else offered.
ssh_dir="$HOME/.ssh"
install -d -m 700 "$ssh_dir"
install -m 600 /dev/null "$ssh_dir/wayland-vnc-ppa"
printf '%s\n' "$key" >"$ssh_dir/wayland-vnc-ppa"
install -m 644 packaging/launchpad-known-hosts "$ssh_dir/wayland-vnc-ppa-known-hosts"
if ! grep -qs '^Host ppa.launchpad.net$' "$ssh_dir/config"; then
  cat >>"$ssh_dir/config" <<SSH
Host ppa.launchpad.net
  IdentityFile $ssh_dir/wayland-vnc-ppa
  IdentitiesOnly yes
  UserKnownHostsFile $ssh_dir/wayland-vnc-ppa-known-hosts
  StrictHostKeyChecking yes
  BatchMode yes
  ConnectTimeout 30
SSH
  chmod 600 "$ssh_dir/config"
fi

dput_cf=$(mktemp)
trap 'rm -f -- "$dput_cf"' EXIT
cat >"$dput_cf" <<CF
[launchpad-sftp]
fqdn = ppa.launchpad.net
method = sftp
incoming = ~$owner/$name
login = $owner
allow_unsigned_uploads = 0
CF
timeout 1800 dput -c "$dput_cf" launchpad-sftp "$changes"
