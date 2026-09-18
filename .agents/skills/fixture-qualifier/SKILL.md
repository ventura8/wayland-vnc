---
name: fixture-qualifier
description: >-
  Build isolated Wayland fixtures and drive the actual RealVNC viewer through the unattended
  scenario suite, recording evidence-gated schema-v2 qualification records.
---

# Fixture Qualifier Skill

Use this skill to run the real compatibility evidence path: an isolated, disposable
compositor fixture plus the actual RealVNC Viewer, driven unattended, producing
schema-v2 records. **Connection success is never compatibility evidence** — only
pixels the viewer rendered and input the scene acknowledged count.

## Hard rules

- Never attach a fixture to the host session, restart its services, reuse its AVD, or
  touch the live GNOME Remote Desktop. Fixtures are throwaway containers with
  `--cap-drop ALL --security-opt no-new-privileges`.
- No Xorg/Xvfb server fallback and no portal bypass. XWayland is replaced by a
  fail-closed stub in every wlroots/labwc fixture.
- Credentials and evidence under `artifacts/` are private and Git-ignored. Never print
  the fixture password or pass it on a command line.
- A missing prerequisite (no DRM render node for Plasma, no VM for suspend/Hyprland)
  is recorded `not-run`/`BLOCKED` with a reason, never a skip or a pass.
- **Qualify only from a clean worktree.** `--commit` binds the record to `HEAD`, so a
  staged, unstaged or untracked change means the record names a tree that was never
  the one under test. Check before every run and refuse if anything is reported:

  ```bash
  git status --porcelain=v1  # must print nothing
  ```

## Fixtures

`docker/Dockerfile.<fixture>` builds each isolated compositor. Smoke one with:

```bash
set -euo pipefail
mkdir -p reports/fixtures
./scripts/fixture-smoke.sh labwc 2>&1 | tee reports/fixtures/labwc.log
```

The smoke checker (`wayland_vnc.fixture_smoke`) requires exactly one compositor
process, one Wayland socket, the server's control socket and TCP listener, one native
scene with a Wayland-only environment, no XWayland process or usable executable, the
genuine desktop-session processes for desktop targets, and one captured output at the
expected mode. It is fixture readiness only.

## Unattended actual-viewer scenarios

`scripts/qualify-desktop.py --harness` (or `scripts/qualify-all.sh` for every target)
runs the installed RealVNC Viewer inside the isolated viewer harness
(`docker/Dockerfile.viewer-harness`) on an internal Docker network — no published
ports, no host desktop, no uinput — injecting keyboard, pointer, scroll, drag, and
session-lock input through WayVNC virtual devices, and writes a record per target:

```bash
set -euo pipefail
scripts/realvnc-connection.sh --credential artifacts/desktop-viewer/fixture.conf \
  --port 5902 --output artifacts/desktop-viewer/qualify-5902.vnc
test -z "$(git status --porcelain=v1)" || { echo "dirty worktree: evidence would not match HEAD" >&2; exit 1; }
PYTHONPATH=src python3 scripts/qualify-desktop.py --fixture sway --port 5902 --harness \
  --identities artifacts/desktop-viewer/identities-5902 \
  --viewer-config artifacts/desktop-viewer/harness-vncviewer.conf \
  --connection artifacts/desktop-viewer/qualify-5902.vnc \
  --credential artifacts/desktop-viewer/fixture.conf \
  --server-key artifacts/desktop-viewer/fixture-rsa.pem \
  --evidence-root artifacts/qualification-evidence --commit "$(git rev-parse HEAD)"
```

Scenarios: first-frame, colors, changing-frames, 1080p, twenty reconnects (with a
bounded transient RA2-handshake retry), viewer-kill, network interruption, server
restart, keyboard, pointer, scroll, drag, session lock/unlock, monitor hot-plug
(every wlroots fixture: Sway creates an output, the others switch a spare headless
one), live resize, 4K@200 %, and the Plasma portal approve/deny/restore decisions.
Every verdict comes from a verified screenshot; after the run the fixture is
smoke-checked again and `post_run_smoke` recorded — a fixture that crashed can never
yield a `passed` record. `suspend-resume` needs a machine: `scripts/qualify-all.sh
--kvm` boots a disposable KVM guest per wlroots target (`scripts/kvm/build-guest.sh`,
one boot per run) and runs the same scenarios there through the harness, S3 included
(docs/testing.md, "KVM guests for every wlroots target").

## Records & release gate

Records are written under the private evidence root's `records/` and validated by
`wayland_vnc.qualification`. A record becomes `passed` only when every required
scenario passed and the fixture stayed healthy. Records reach the release gate through
the private evidence store (`docs/evidence-store.md`: `scripts/publish-evidence.py`
copies the records for `HEAD` and their artifacts into `<store>/<commit>/`), never
through the in-repo `qualification/records.json`, which stays empty. Evidence is bound
to the commit it was made on, so run the pairs on the frozen release commit -- an
amend afterwards makes every record stale. The gate stays red until all fourteen
target/viewer pairs have valid evidence:

```bash
PYTHONPATH=src python3 scripts/check-release.py \
  --commit "$(git rev-parse HEAD)" --evidence-root /private/qualification-evidence
```

Do not claim a desktop is supported until its complete qualification suite passes.
