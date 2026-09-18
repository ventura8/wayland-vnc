# The qualification evidence store

Qualification evidence is produced on a trusted, isolated runner (`scripts/qualify-desktop.py`,
`scripts/android-lab.py`) and is private: it holds viewer credentials, screen captures and
packet-level logs. It is never committed to this repository, and the release workflow runs
on a hosted machine that cannot see the runner's disk. The store is how the two meet.

## Layout

The store is a private git repository (`ventura8/wayland-vnc-evidence`, named in
`.github/workflows/release.yml` as `EVIDENCE_REPO`). It holds one directory per commit,
laid out exactly like an evidence root, so the gate runs the same `check-release.py` call
the local pipeline runs:

```text
<commit-sha>/
  records.json                     every record for that commit, as one JSON list
  records/<target>-<viewer>-<run-id>.json   the records as the runner wrote them
  <target>/<viewer>/<run-id>/...   the captures, input results and logs they reference
```

Records reference artifacts by path relative to the evidence root and by SHA-256, and
`check-release.py` verifies both; the layout keeps those paths valid unchanged.

## Publishing (trusted runner)

After a qualification run has written its record under `artifacts/qualification-evidence/`:

```bash
git clone git@github.com:ventura8/wayland-vnc-evidence.git ../wayland-vnc-evidence   # once
PYTHONPATH=src python3 scripts/publish-evidence.py --store ../wayland-vnc-evidence --push
```

`publish-evidence.py` takes the commit from `HEAD` (or `--commit`), selects the records for
that commit, verifies every referenced artifact against the hash in its record, copies
records and artifacts into `<store>/<commit>/`, writes `records.json`, and with `--push`
commits and pushes with the runner's own git credentials. Only records that already
validate are published by default; `--include-incomplete` also carries partial records so
progress on a target is visible next to the complete pairs (the gate rejects them exactly as
it would locally). An artifact that no longer matches its record stops the publication:
that is corruption, not an incomplete run.

Evidence must be for the commit that will be tagged. Freeze the release candidate first,
run every target/viewer pair against that SHA, publish, then tag.

## The campaign, machine by machine

The fourteen pairs come from KVM guests (docs/testing.md, "KVM guests for every
wlroots target"): a container can never supply `suspend-resume`. Two machines share the
work, and the records meet in one evidence root before publishing:

1. Sync the frozen tree to the lab PC and run the seven desktop-viewer pairs there,
   naming the commit because the synced tree has no `.git`:
   `WAYLAND_VNC_COMMIT=<sha> scripts/qualify-all.sh --kvm` (guests are built and
   discarded per target; about 15 minutes each).
2. On the laptop, which has the Android emulator, run the seven Android pairs:
   `scripts/qualify-all.sh --android --kvm` (about 50 minutes each; the emulator must be
   the only heavy load on the machine).
3. Bring the PC's records and artifacts into the laptop's evidence root, structure
   intact:
   `rsync -a lab-pc:wayland-vnc/artifacts/qualification-evidence/ artifacts/qualification-evidence/`
4. `PYTHONPATH=src python3 scripts/publish-evidence.py --store ../wayland-vnc-evidence --push`,
   which verifies every record's artifacts against their hashes before anything is
   copied, then tag.

## The gate (release workflow)

`qualification-gate` checks the store out read-only with a deploy key, sparse to the tagged
commit's directory, and runs

```bash
scripts/check-release.py --records evidence-store/<sha>/records.json \
  --commit <sha> --evidence-root evidence-store/<sha>
```

Three outcomes, none of which fail the tag:

- no key or no directory for this commit: `qualified=false`, "no records for this commit";
- records present but the contract not met: `qualified=false`, with the verdict in the log;
- all fourteen pairs valid for this commit: `qualified=true`.

`qualified` decides only whether the GitHub release is a prerelease and which line the PPA
changelog carries; it never decides whether the PPA upload or the release happens.

## One-time setup (maintainer)

1. Create the private repository `ventura8/wayland-vnc-evidence` (an empty `main` is fine).
2. Generate a key pair for the gate and register the public half as a **read-only** deploy
   key on the evidence repository:

   ```bash
   ssh-keygen -t ed25519 -N "" -C "wayland-vnc release gate (read-only)" -f "$HOME/.ssh/wayland-vnc-evidence-gate"
   gh repo deploy-key add "$HOME/.ssh/wayland-vnc-evidence-gate.pub" --repo ventura8/wayland-vnc-evidence --title "wayland-vnc release gate"
   ```

3. Store the private half as the repository secret the workflow reads, then remove the
   local copy of the private key:

   ```bash
   gh secret set EVIDENCE_DEPLOY_KEY --repo ventura8/wayland-vnc < "$HOME/.ssh/wayland-vnc-evidence-gate"
   shred -u "$HOME/.ssh/wayland-vnc-evidence-gate"
   ```

The key can only read one private repository; it cannot push evidence, and it cannot reach
anything else. Pushing is done from the trusted runner with the maintainer's own
credentials. This setup was completed for `ventura8/wayland-vnc` on 2026-09-17 (repository
created with an empty `main`, deploy key "wayland-vnc release gate" registered read-only,
`EVIDENCE_DEPLOY_KEY` stored at repository scope).
