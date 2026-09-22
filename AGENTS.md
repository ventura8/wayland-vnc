# Project Agent Rules & Development Guidelines

## Agent compatibility

This file is the canonical, tool-neutral instruction set for **any** coding agent
working in this repository. It follows the cross-vendor `AGENTS.md` convention, and
nothing in it is specific to one vendor or product.

- Agents that look for a different filename are pointed here by thin stubs that only
  redirect, so the rules live in exactly one place and cannot drift:
  [`CLAUDE.md`](CLAUDE.md), [`.github/copilot-instructions.md`](.github/copilot-instructions.md),
  and [`.cursor/rules/wayland-vnc.mdc`](.cursor/rules/wayland-vnc.mdc).
- The runbooks under [`.agents/skills/`](.agents/skills) are plain Markdown with YAML
  front matter. They assume no particular agent runtime, tool API, or slash command:
  every step is an ordinary shell command any agent (or human) can run. Read the
  relevant `SKILL.md` and follow it directly.
- Where a rule matters, it is stated as a rule rather than a tool invocation, so an
  agent without a given capability can still comply.

## Project overview

`wayland-vnc` is an evidence-gated, Wayland-only VNC compatibility toolkit that makes
native Wayland desktops reachable with the **actual RealVNC Viewer** on desktop and
Android. It ships a Python CLI (diagnostics, provisioning, and the installed serving
path), hardened native patch series for GNOME Remote Desktop / LibVNCServer / TigerVNC,
isolated Docker compositor fixtures, an unattended actual-viewer qualification harness,
and full multi-format packaging (deb, rpm, Arch, AppImage, Flatpak, Snap) with PPA and
GitHub release automation.

- **Version single source of truth**: the release number lives only in the repo-root
  [`VERSION`](VERSION) file (currently `1.0.0`, displayed as `v1.0.0`). After bumping
  `VERSION`, run [`scripts/sync-version.py`](scripts/sync-version.py) so
  `pyproject.toml` `[project] version` matches; `--full` runs `--check` and fails if
  they diverge. The deb/rpm/Arch builders read `VERSION` at build time. Cut notes with
  [`.agents/skills/release/SKILL.md`](.agents/skills/release/SKILL.md) (version from the
  current branch; reset `PPA_UPLOAD_REVISION` to `1` on a new `VERSION`).

## Safety rules (mandatory)

- Preserve the working laptop: never restart, replace, or reconfigure its remote
  desktop during development, and never enable the packaged service on the host as part
  of building or testing it. Build and exercise packages only in throwaway containers.
- Never switch the host desktop to X11 and never install or use an Xorg VNC server
  fallback. All server fixtures are native Wayland; no Xvfb server fallback, no portal
  bypass. XWayland is replaced by a fail-closed stub in wlroots/labwc fixtures.
- Connection success is **not** compatibility evidence: actual RealVNC pixels and input
  must pass. Do not claim a desktop is supported until its complete qualification suite
  passes on a trusted runner and its record is published to the evidence store for the
  exact commit that gets tagged (`docs/evidence-store.md`); an amend after the run
  invalidates every record.
- Mark missing tests/prerequisites `blocked` / `not-run` with a reason — never a pass
  or a silent skip. No stable release before all targets qualify.
- Never commit credentials, APKs, viewer binaries, personal screenshots, keys, or
  troubleshooting logs. `artifacts/` is Git-ignored; the fixture password is never
  printed or passed on a command line.
- Keep correctness patches separate from compatibility workarounds and preserve
  upstream licensing. Build native code only from checksum-verified upstream archives.
- The server binds **this machine only** (`127.0.0.1:5900`) by default. Reaching it
  from a phone on the LAN is an explicit opt-in -- the settings app's **Local Network
  Access** switch (`Actions.set_lan_access`), which binds every interface -- and that
  reaches every network the machine is on, because a systemd **user** unit cannot
  fence traffic: `IPAddressAllow` is applied only by a privileged manager (verified
  on this laptop: the journal says "not running as root", `bpftool` shows no program
  on the unit's cgroup). Never claim a kernel-level fence, never make the wildcard
  bind the default, never recommend Internet exposure. On GNOME the private daemon
  (`wayland-vnc-grd`) reads `WAYLAND_VNC_LISTEN_ADDRESS`; the distribution's listens
  everywhere and the switch says so. Authentication is always on: `serve` generates
  a random mode-600 credential on first run rather than ever serving unauthenticated.
- Do not run untrusted pull requests on personal or credential-bearing runners.

## Python runs in a virtual environment

Every Python the project runs by hand or from a script -- development, the unit
suite, lints, debugging tools, the qualification runners, the release gate, the
hardware validation on a real machine, work on a lab machine -- runs from a virtual
environment built from `requirements-dev.txt`: `scripts/ensure-venv.sh` creates or
refreshes `.venv` (idempotent) and prints its `bin` directory, and every host-side
entry point in `scripts/` puts that directory first on `PATH`. Never `pip install`
into the system interpreter, never `--user`, never `--break-system-packages`, never
run a check with whatever tool version the host happens to have: the pins are the
toolchain CI uses, and a result from a different one is not a result. The venv
inherits the distribution's site-packages on purpose (PyGObject and the GTK4/libadwaita
typelibs are system packages the settings app imports). The one Python that runs
outside a venv is the installed product itself, whose dependencies its package
declares to the distribution's package manager.

## Documentation moves with the code

Every change updates the Markdown it affects, in the same change set: `AGENTS.md`
and the skills when a rule or runbook changes, `docs/` for behaviour, testing,
packaging and lab procedure, `README.md` for anything a user sees, the release notes
under `docs/releases/` for anything shipped, and `docs/upstream/` for a defect found
in a dependency. A finding, a decision or a new script that is not written down where
the next person will look is unfinished work, exactly like a module without tests.

## No suppressions allowed

Never silence a linter anywhere: no `# noqa`, `# pylint: disable`, `# type: ignore`,
`# shellcheck disable`, `eslint-disable`, and **no per-file-ignore config of any kind**
(no `[tool.ruff.lint.per-file-ignores]` or equivalent). Fix or restructure the code; if
a whole rule category is wrong for the project, drop it from the global `select` (as
ruff's bandit `S` category was), never ignore it per line or per file. This covers
SonarQube too: never resolve an issue as "won't fix" or "false positive" on the
dashboard, and never add a `NOSONAR` comment -- turn the rule off in the quality
profile, with a reason, or fix the code. See
[`.agents/skills/code-linter/SKILL.md`](.agents/skills/code-linter/SKILL.md).

## New files must be linted and tested

A change set that adds a file must lint and test that path in the same change set —
new paths are not exempt from any gate. Product code ships with tests; a new module
without tests, or a translated/packaged path without its smoke, is incomplete work.

## Strict linting requirements

- **Python**: `ruff check` (rules `E, F, I, B, UP`, line length 100),
  `ruff format --check`, and `pylint --persistent=n src/wayland_vnc` at 10.00/10.
- **Shell**: `shellcheck` + `shfmt -i 2`; every script is `set -euo pipefail` with
  bounded `timeout` and reliable process-group / container cleanup.
- **Containers/CI/Docs**: `hadolint` (digest-pinned bases), `actionlint`, `yamllint`,
  and markdownlint (line length 140, wide enough for a table row), all via
  `scripts/lint-containers.sh`.
- **Static analysis**: SonarQube Cloud (`ventura8_wayland-vnc`), locally with
  `scripts/run-sonar-scan.sh` and in CI's `sonar` job, both reading the one
  `sonar-project.properties` so the two agree. The quality gate is blocking in CI.

## Local pipeline & CI parity

`./scripts/build-and-test.sh --full` is the single local entry point (it runs from
the project venv, see above) and mirrors CI:
version-sync check, the full lint set, the native ASan/UBSan test, the systemd-unit
round trip, the unit suite with aggregate and per-file ≥90 % coverage, every fixture
smoke, and the deb package smoke. `--unit` is the quick loop. See
[`.agents/skills/pipeline-runner/SKILL.md`](.agents/skills/pipeline-runner/SKILL.md).

`.github/workflows/ci.yml` runs lint, unit+coverage, native sanitizers, the fixture
smoke matrix, and the package-smoke matrix on every push and PR. Public PR CI must
never receive credentials or the private evidence directory.

## Packaging & release

- The installed product is a real Wayland VNC server: the package installs WayVNC, a
  hardened systemd **user** service (`packaging/systemd/wayland-vnc.service`), the
  provisioning and diagnostic CLI, and the public qualification schema. All formats
  share [`packaging/stage-payload.sh`](packaging/stage-payload.sh).
- Install / uninstall are smoke-tested in throwaway containers across every packaged
  format, happy and failure paths, by `scripts/run_deb_package_smoke.sh`,
  `scripts/run_rpm_package_smoke.sh`, `scripts/run_arch_package_smoke.sh`, and the
  shared `scripts/package_smoke_scenarios.sh`.
- `.github/workflows/release.yml` triggers on a `vX.Y.Z` tag: it validates
  `VERSION` == tag == pyproject, builds and signs the Debian source package and
  `dput`s it to `ppa:ventura8/wayland-vnc` (resolute), builds every binary format, and
  publishes a GitHub Release with a `SHA256SUMS` manifest. `PPA_UPLOAD_REVISION` bumps
  on a re-upload of the same `VERSION` and resets to `1` on a new `VERSION`.

## Qualification & evidence

Actual RealVNC compatibility is proved by the fixture-qualifier flow, not by unit
tests or a connection. See
[`.agents/skills/fixture-qualifier/SKILL.md`](.agents/skills/fixture-qualifier/SKILL.md)
and [`.agents/skills/native-patch-verifier/SKILL.md`](.agents/skills/native-patch-verifier/SKILL.md).
The release gate (`scripts/check-release.py`) reads the evidence store's
`<commit>/records.json` (`docs/evidence-store.md`) and stays red until all fourteen
target/viewer pairs pass on the tagged commit; the in-repo `qualification/records.json`
stays empty and is only the local default.

## Agent skills

Skills live under `.agents/skills/*/SKILL.md`:

- [`code-linter`](.agents/skills/code-linter/SKILL.md) — lint everything, no suppressions.
- [`test-runner`](.agents/skills/test-runner/SKILL.md) — unit + fixture + native tests, ≥90% coverage.
- [`pipeline-runner`](.agents/skills/pipeline-runner/SKILL.md) — the local CI-parity pipeline.
- [`fixture-qualifier`](.agents/skills/fixture-qualifier/SKILL.md) — actual-viewer qualification.
- [`native-patch-verifier`](.agents/skills/native-patch-verifier/SKILL.md) — patch series,
  sanitizers and units.
- [`release`](.agents/skills/release/SKILL.md) — release docs, version pins, PPA revision.
- [`resolve-pr-comments`](.agents/skills/resolve-pr-comments/SKILL.md) — resolve every PR
  review thread.
- [`review-with-coderabbit`](.agents/skills/review-with-coderabbit/SKILL.md) — user-gated
  CodeRabbit review.
