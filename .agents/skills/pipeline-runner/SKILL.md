---
name: pipeline-runner
description: >-
  Run the local wayland-vnc pipeline (lint, unit+coverage, native sanitizers, fixture smokes,
  package smokes) with live output and persistent logs.
---

# Local Pipeline Runner Skill

Validate code quality, tests, coverage, fixtures, and packaging locally before
committing. Mirror what CI runs so a green local run predicts a green pipeline.

## Instructions

1. **New paths first.** When a change set adds files, lint and test those paths (see
   the `code-linter` and `test-runner` skills) before running the whole pipeline.

2. **Full pipeline** — the same entry point CI uses, with live streaming and a saved
   log:

   ```bash
   set -euo pipefail
   mkdir -p reports/pipeline
   timeout 5400 ./scripts/build-and-test.sh --full 2>&1 | tee reports/pipeline/full.log
   ```

   `--full` runs, in order: version-sync check (`scripts/sync-version.py --check`),
   ruff + pylint + shellcheck + shfmt + yamllint, container lint
   (`scripts/lint-containers.sh`), the native ASan/UBSan test
   (`scripts/test-native.sh`), the systemd-unit round trip
   (`scripts/test-units.sh`), the unit suite with per-file ≥90 % coverage, every
   fixture smoke (`scripts/fixture-smoke.sh` for sway, labwc, xfce-labwc, lxqt-labwc,
   wayfire, gnome; plasma when a DRM render node exists, else reported BLOCKED), and
   the deb package smoke (`scripts/run_deb_package_smoke.sh`). It preserves the real
   exit status.

3. **Package smokes** — build and install/uninstall every packaged format in
   throwaway containers, covering happy and failure scenarios:

   ```bash
   set -euo pipefail
   mkdir -p reports/pipeline
   ./scripts/run_deb_package_smoke.sh      2>&1 | tee reports/pipeline/deb-smoke.log
   ./scripts/run_rpm_package_smoke.sh      2>&1 | tee reports/pipeline/rpm-smoke.log
   ./scripts/run_arch_package_smoke.sh     2>&1 | tee reports/pipeline/arch-smoke.log
   ./scripts/run_portable_package_smoke.sh 2>&1 | tee reports/pipeline/portable-smoke.log
   ```

   Every packaged format is covered, and none may be declared smoked without its
   driver having run:

   | Format | Driver | Install/uninstall coverage |
   | --- | --- | --- |
   | deb | `run_deb_package_smoke.sh` | real `apt install` + purge |
   | rpm | `run_rpm_package_smoke.sh` | real `rpm -i` + erase |
   | Arch | `run_arch_package_smoke.sh` | real `pacman -U` + removal |
   | AppImage | `run_portable_package_smoke.sh` | extracted AppRun + zero-footprint check |
   | Snap | `run_portable_package_smoke.sh` | unsquashed payload + zero-footprint check |
   | Flatpak | `run_portable_package_smoke.sh` | manifest + the exact `/app` payload |

   Each deb/rpm/Arch driver builds the package, installs it, runs
   `scripts/package_smoke_scenarios.sh` (the platform-agnostic happy+bad-path suite),
   then upgrades/reinstalls and removes/purges and asserts nothing is left behind. The
   portable driver runs the same scenario suite against the AppImage and the snap
   payload and rejects a truncated image.

   Two exclusions are deliberate, and they are limits of the container, not formats
   left unsmoked: a real `snap install` needs snapd plus systemd as PID 1, and a real
   `flatpak-builder`/`flatpak run` cycle needs the GNOME runtime and bubblewrap user
   namespaces. Both are host/CI work. Do not call package smoke complete until the
   portable driver has run and printed those two NOTE lines.

4. **Single fixture** while iterating:

   ```bash
   set -euo pipefail
   mkdir -p reports/pipeline
   ./scripts/fixture-smoke.sh labwc 2>&1 | tee reports/pipeline/labwc.log
   ```

5. **Actual-viewer qualification** is a separate, credential-bearing flow — see the
   `fixture-qualifier` skill. Never run it on a public runner.

## Rules

- The pipeline runs from the project venv: `build-and-test.sh` calls
  `scripts/ensure-venv.sh` itself; when running steps by hand, put
  `$(scripts/ensure-venv.sh)` first on `PATH`. Never install the toolchain into the
  system interpreter (AGENTS.md, "Python runs in a virtual environment").
- Update the Markdown a change affects in the same change set (AGENTS.md,
  "Documentation moves with the code"); the pipeline lints it, it does not write it.
- Always `tee` into `reports/` so the user can watch live and inspect afterwards.
- Treat a package-smoke failure as a real install/uninstall bug, not flake.
- Do not weaken a gate (coverage, lint, smoke) to make the pipeline pass.
