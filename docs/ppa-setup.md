# PPA upload: what the maintainer has to do once

Everything in the release workflow is already written. The PPA leg is the only part
that cannot be exercised from CI or from this repository, because it needs a Launchpad
account and a signing key that only the maintainer holds. This is the whole list.

## What the workflow does at a tag

`.github/workflows/release.yml`, job `upload-to-ppa`:

1. refuses a tag that is not an ancestor of the default branch, and a tag that
   disagrees with the `VERSION` file;
2. imports the signing key from the `GPG_PRIVATE_KEY` secret;
3. rewrites `debian/changelog` to `<version>+1ppa<revision>~<series>1` for the series
   in the matrix (`resolute`, Ubuntu 26.04);
4. builds a source-only package and signs it with that key, using `GPG_PASSPHRASE`;
5. runs `dput ppa:ventura8/wayland-vnc` on the resulting `.changes`.

Steps 3 and 4 are already verified locally: `scripts/run_ppa_source_smoke.sh` builds
the same source package unsigned in a clean Ubuntu 26.04 container and checks the
generated changelog parses, the version sorts correctly, the upload is source-only
with a `.dsc` and a native tarball, and lintian reports no errors. It runs in the
`--full` and `--packages` gates.

## One-time setup

### 1. Launchpad account and code of conduct

A Launchpad account named `ventura8` (the workflow's `PPA_NAME` is
`ppa:ventura8/wayland-vnc`; change the workflow if the account differs). Launchpad
refuses uploads from anyone who has not signed the Ubuntu Code of Conduct, which is
done from the account page and requires the GPG key from step 3.

### 2. The PPA itself

Create a PPA named exactly `wayland-vnc` at
<https://launchpad.net/~ventura8/+activate-ppa>. Its display name and description are
free text; the URL name is what `dput` addresses.

### 3. An OpenPGP key Launchpad trusts

```bash
gpg --full-generate-key          # RSA 4096, no expiry or a long one, with a passphrase
gpg --list-secret-keys --keyid-format=long   # note the fingerprint
gpg --send-keys --keyserver keyserver.ubuntu.com <FINGERPRINT>
```

Then add the fingerprint at <https://launchpad.net/~ventura8/+editpgpkeys>. Launchpad
sends an encrypted confirmation mail; decrypting and following it is what proves the
key is yours.

### 4. The two GitHub secrets, in the `ppa-release` environment

The upload job runs under `environment: ppa-release`, so the secrets live there rather
than at repository scope, and the environment can require a reviewer: with one, no PPA
upload leaves GitHub until a maintainer approves that run.

```bash
gh api --method PUT /repos/ventura8/wayland-vnc/environments/ppa-release \
  -f 'reviewers[][type]=User' -F "reviewers[][id]=$(gh api user -q .id)"
umask 077
gpg --armor --export-secret-keys <FINGERPRINT> > "$HOME/ppa-key.asc"
gh secret set GPG_PRIVATE_KEY --repo ventura8/wayland-vnc --env ppa-release < "$HOME/ppa-key.asc"
shred -u "$HOME/ppa-key.asc"
gh secret set GPG_PASSPHRASE --repo ventura8/wayland-vnc --env ppa-release  # prompts; nothing on argv
gh secret list --repo ventura8/wayland-vnc --env ppa-release                # both names listed
```

The key needs a non-empty passphrase: the signing step refuses an empty one. The
workflow reads only these two secrets. It never prints them, and the key file it writes
is removed by a trap.

### 5. Prove the signing path once, without uploading

`scripts/run_ppa_source_smoke.sh` builds the source package unsigned in a container;
what it cannot exercise is this key. Sign a throwaway build with it and let `dput`
check the signatures and address the PPA, stopping short of the upload:

```bash
tmp=$(mktemp -d) && git archive --format=tar HEAD | tar -x -C "$tmp" && cd "$tmp"
dpkg-buildpackage -S -sa -d -us -uc
debsign -k <FINGERPRINT> ../wayland-vnc_<version>_source.changes
dput --simulate ppa:ventura8/wayland-vnc ../wayland-vnc_<version>_source.changes
```

Expected: "Valid signature" for both `.changes` and `.dsc`, then "Simulated upload."
Never drop `--simulate` here: Launchpad rejects a source version it has seen once.

## Two things to know before the first tag

**The PPA tracks every tag, unattended.** The maintainer decided (2026-09-17) that
`upload-to-ppa` runs on every release tag without waiting for the qualification gate
or for a manual approval. What the gate found is not hidden from subscribers: the
generated changelog carries either `Release-qualified: evidence for every
target/viewer pair on this commit` or `Not release-qualified: no qualification evidence
for this commit`, and the GitHub release keeps its prerelease flag until the evidence
exists. Only the `wayland-vnc` source package goes to the PPA; the GNOME backend
`wayland-vnc-grd` remains a release asset.

**Re-uploading the same version.** Launchpad rejects a source version it has seen
before, even after deleting it. Bump `PPA_UPLOAD_REVISION` in the workflow's `env`
block for a second upload of the same `VERSION`, and reset it to `1` when `VERSION`
itself changes.

## Checking the result

After a successful upload Launchpad mails an acceptance notice and starts a build at
<https://launchpad.net/~ventura8/+archive/ubuntu/wayland-vnc/+packages>. A rejection
mail almost always means one of: unsigned or unknown key, an unsigned Code of Conduct,
a version already present, or a series that does not exist. The first three are the
steps above; the fourth is the `distro` matrix entry.
