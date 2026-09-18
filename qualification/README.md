# Qualification records

`records.json` here stays empty on purpose: development fixtures and connection-only
results are not release qualifications, and complete trusted-runner records are not
committed to this repository at all. They are published to the private evidence store
(`docs/evidence-store.md`), whose `<commit>/records.json` is what the release gate
reads for the tagged commit.

Records use `schema-v2.json`. Each record is bound to an exact Git commit and
content-addressed synthetic artifacts. The validator rejects missing targets,
duplicate target/viewer pairs, X11 sessions, skipped scenarios, stale commits,
symlinks, directory escapes, size mismatches, and hash mismatches.

Automated records are produced by `scripts/qualify-desktop.py` (one target) or
`scripts/qualify-all.sh` (every buildable target) into the private evidence
root's `records/` directory. With `--harness` the runner drives the actual
RealVNC Viewer inside an isolated container and injects keyboard, pointer,
scroll, drag and session-lock input, so those scenarios are covered without a
person; otherwise they stay `not-run` until attended evidence is merged with
`scripts/record-manual-input.py`. A record becomes `passed` only when every
required scenario passed and the fixture stayed healthy afterwards, and only
records that validate against `schema-v2.json` are published to the store
(`scripts/publish-evidence.py`). Records are bound to the commit they were made on:
run the pairs on the frozen release commit, since an amend afterwards stales them all.
`suspend-resume` needs a VM and Hyprland is blocked, so no target reaches a full
`passed` record from a container alone.

Run the release gate with an explicit commit and private evidence directory:

```sh
PYTHONPATH=src python3 scripts/check-release.py \
  --commit "$(git rev-parse HEAD)" \
  --evidence-root /private/qualification-evidence
```

The empty record set must fail with fourteen missing target/viewer pairs. Actual
viewer jobs run only on disposable trusted infrastructure; public pull requests
must never receive credentials or access to the private evidence directory.
