---
name: code-linter
description: >-
  Run ruff, pylint, shellcheck, shfmt, yamllint, markdownlint, hadolint, actionlint, and the
  SonarQube Cloud analysis over wayland-vnc without suppressions or per-file ignores.
---

# Code Linter Skill

Lint every Python module, shell script, container file, YAML workflow, and Markdown
document in this repository. **No suppressions of any kind are allowed** — never add
`# noqa`, `# pylint: disable`, `# type: ignore`, `# shellcheck disable`, an
`ignore` directive, a `NOSONAR` comment, a SonarQube issue resolved as "won't fix" or
"false positive", or a `[tool.ruff.lint.per-file-ignores]` (or any other per-file
ignore) section. Fix the code or restructure it instead; a rule that is noisy for a
legitimate pattern is removed from the global rule set (as `ruff`'s bandit `S` rules
were), never silenced on one line or one file.

## New files

New paths are not exempt from any gate. When a change set adds a file, lint it before
finishing — run the matching tool directly (`ruff check` + `pylint` for Python,
`bash -n` + `shellcheck` + `shfmt` for shell, `hadolint` for a Dockerfile).

## Instructions

1. **From the venv.** `export PATH="$(scripts/ensure-venv.sh):$PATH"` first: the
   pinned `ruff`, `pylint`, `pymarkdown` and `yamllint` live there, and a lint from a
   different version is not the gate's verdict (AGENTS.md, "Python runs in a virtual
   environment").

2. **Autofix first.** Before hand-editing a lint failure run the safe autofixers and
   re-lint: `ruff check --fix src tests scripts`, `ruff format src tests scripts`,
   `shfmt -w -i 2 scripts/*.sh packaging/*.sh`. Only then fix the rest by hand.

3. **Python.** `ruff check src tests scripts`, `ruff format --check src tests scripts`,
   and `pylint --persistent=n src/wayland_vnc` (pylint runs on the product module; the
   fixtures under `tests/fixtures/` are runtime programs executed inside containers,
   not import targets). `pylint` must stay at **10.00/10**. Ruff's selected rule set is
   `E, F, I, B, UP` (line length 100). If a rule fires, change the code; if a whole
   category is inappropriate for the project, drop it from `select` in `pyproject.toml`
   — do not ignore it per file.

4. **Shell.** `shellcheck scripts/*.sh packaging/*.sh` and `shfmt -d -i 2` on the same
   set. Every script is `set -euo pipefail`, uses bounded `timeout` around external
   waits, and cleans up its containers/process groups on exit.

5. **Containers.** `bash scripts/lint-containers.sh` runs `hadolint` on every
   `docker/Dockerfile.*` and `actionlint` on the workflows, in pinned tool images with
   no network. Every `Dockerfile.*` pins its base image by digest.

6. **YAML + Markdown.** `yamllint .github .yamllint.yaml` and the markdownlint pass in
   `scripts/lint-containers.sh` (line length 100, artifacts and vendor excluded).

7. **Static analysis.** `scripts/run-sonar-scan.sh` analyses the tree with SonarQube
   Cloud (project `ventura8_wayland-vnc`) and uploads the coverage from the same pinned
   pytest run, using the settings in `sonar-project.properties` that CI's `sonar` job
   also reads. It needs a user token in `SONAR_TOKEN`; generate one at
   <https://sonarcloud.io/account/security> and never commit it. Findings are fixed in
   the code — see the no-suppressions rule above, which covers the dashboard too.

8. **Live output + logs.** Stream with `tee` into `reports/lint/` so a reviewer can
   watch and re-read:

   ```bash
   set -euo pipefail
   mkdir -p reports/lint
   ./scripts/build-and-test.sh --full 2>&1 | tee reports/lint/full.log
   ```

## Gate

`./scripts/build-and-test.sh --full` runs the whole lint set plus the native
sanitizer build, the unit suite with per-file ≥90 % coverage, every fixture smoke,
and the deb package smoke. Lint work is not complete until that command exits 0 with
no suppression anywhere in the diff.
