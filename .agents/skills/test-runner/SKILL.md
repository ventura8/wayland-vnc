---
name: test-runner
description: >-
  Run the wayland-vnc unit suite, fixture smokes, and native sanitizer tests with ≥90% per-file
  coverage; keep real boundaries real.
---

# Test Runner Skill

Execute the automated tests and measure coverage. The unit suite is **not** desktop
qualification — actual RealVNC pixel and input evidence is gated separately (see the
`fixture-qualifier` skill).

Run everything from the project venv: `export PATH="$(scripts/ensure-venv.sh):$PATH"`
(the gate script does this itself). A system-interpreter run is not a result, and a
lab machine without `pip` access needs the same venv (`python3 -m venv
--system-site-packages`). The settings-app tests need Xvfb and error out without it —
that is a blocked environment, not a failing suite.

## Dependency & mocking philosophy

- **Real boundaries stay real.** Unit fakes may stand in only for external boundaries
  that cannot run in a plain container: a live compositor, WayVNC, the RealVNC Viewer,
  OpenSSL, `docker`, `/proc`, and sockets. Inject those through parameters (see
  `Driver`/`Host` dataclasses in `desktop_scenarios.py` and `fixture_smoke.py`) so the
  logic is tested without them.
- **Never mock owned code.** Do not stub functions, classes, or modules from
  `src/wayland_vnc/`. Call the real implementation and inject only the boundary.
- **No fabricated passes.** A missing prerequisite is recorded as `blocked` /
  `not-run` with a reason, never turned into a silent skip or a green result. E2E
  scenarios read pixels the actual viewer rendered.

## New files

Product code and tests land in the same change set. A new module without tests is
incomplete work. Run the module's tests before finishing.

## Commands

```bash
set -euo pipefail
# Fast developer loop: unit tests + coverage report.
PYTHONPATH=src python3 -m coverage run -m pytest -q
python3 -m coverage report
# Per-file gate (fails if any owned file is < 90%).
python3 -m coverage json -o coverage.json
python3 scripts/check-coverage.py coverage.json
```

- `scripts/build-and-test.sh --unit` is the quick loop; `--full` adds lint, the native
  ASan/UBSan build (`scripts/test-native.sh`), the systemd-unit round trip
  (`scripts/test-units.sh`), every fixture smoke, and the deb package smoke.
- Native buffer-layout regressions build under `-fsanitize=address,undefined`.
- Fixture smokes (`scripts/fixture-smoke.sh FIXTURE`) prove a disposable compositor
  fixture started correctly; they are never viewer compatibility evidence.
- Coverage gates: aggregate `fail_under = 90` in `pyproject.toml` **and** per-file
  ≥90 % via `scripts/check-coverage.py`. Both must hold.

## Live output & logs

Stream with `tee` into `reports/tests/` so runs can be watched and re-read:

```bash
set -euo pipefail
mkdir -p reports/tests
PYTHONPATH=src python3 -m pytest -q 2>&1 | tee reports/tests/unit.log
```
