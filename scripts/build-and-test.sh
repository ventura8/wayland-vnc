#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
# Every Python step below -- and every script this one calls -- runs from the project
# venv with the pinned toolchain, never from whatever the system interpreter has.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH
case "${1:---full}" in
--unit)
  python3 -m coverage run -m pytest
  python3 -m coverage report
  ;;
--full)
  python3 scripts/sync-version.py --check
  ruff check src tests scripts
  ruff format --check src tests scripts
  pylint --persistent=n src/wayland_vnc
  # Documentation is linted like code: 43 Markdown files, no exemptions.
  pymarkdown -c .pymarkdown.json scan AGENTS.md CLAUDE.md README.md SECURITY.md \
    .github/copilot-instructions.md docs .agents
  # Every shell script, including scripts/kvm/, which a top-level glob never reached.
  mapfile -t shell_scripts < <(find scripts packaging -type f -name '*.sh' | sort)
  shellcheck "${shell_scripts[@]}"
  shfmt -d -i 2 "${shell_scripts[@]}"
  yamllint .github .yamllint.yaml packaging/flatpak packaging/snap
  # Translations are gated like code: the template must match the marked strings, and
  # every shipped catalogue must compile. A stale template means a new string reaches
  # users untranslated in all 101 languages.
  bash scripts/build-translations.sh check
  # A missing, fuzzy or empty translation in ANY language fails the build: the user
  # sees English for that one string, and nothing else would catch it.
  python3 scripts/i18n-lint.py
  bash scripts/build-translations.sh compile
  bash scripts/lint-containers.sh
  bash scripts/test-native.sh
  bash scripts/test-units.sh
  python3 -m coverage run -m pytest
  python3 -m coverage report
  python3 -m coverage json
  python3 scripts/check-coverage.py coverage.json
  for fixture in sway labwc xfce-labwc lxqt-labwc wayfire gnome; do
    bash scripts/fixture-smoke.sh "$fixture"
  done
  # Plasma needs a DRM render node. Without one the smoke is BLOCKED, and a blocked
  # check is not a passed one: the gate fails rather than let a green run stand in
  # for evidence that was never gathered.
  bash scripts/fixture-smoke.sh plasma || {
    status=$?
    if ((status == 3)); then
      echo "BLOCKED: plasma fixture smoke not run on this machine (no DRM render node);" \
        "the full gate does not pass without it" >&2
    fi
    exit "$status"
  }
  bash scripts/run_deb_package_smoke.sh
  bash scripts/run_grd_package_smoke.sh
  bash scripts/run_ppa_source_smoke.sh
  # The gate must reach a verdict and say no: a gate that exits non-zero before
  # judging (a missing module, a usage error) would otherwise "fail" as expected.
  verdict=$(PYTHONPATH=src python3 scripts/check-release.py \
    --commit unit-test-commit --evidence-root artifacts/qualification-empty) &&
    {
      echo "Empty qualification records unexpectedly passed" >&2
      exit 1
    }
  grep -q '"passed": false' <<<"$verdict" ||
    {
      echo "check-release.py did not reach a verdict on the empty records" >&2
      exit 1
    }
  ;;
--packages)
  bash scripts/run_deb_package_smoke.sh
  bash scripts/run_grd_package_smoke.sh
  bash scripts/run_ppa_source_smoke.sh
  bash scripts/run_rpm_package_smoke.sh
  bash scripts/run_arch_package_smoke.sh
  bash scripts/run_portable_package_smoke.sh
  bash scripts/run_service_activation_smoke.sh
  ;;
--fixtures)
  # The developer loop on the lab machine requires every startable fixture, Plasma included.
  for fixture in sway labwc xfce-labwc lxqt-labwc wayfire gnome plasma; do
    bash scripts/fixture-smoke.sh "$fixture"
  done
  ;;
*)
  echo "Usage: $0 [--unit|--full|--packages|--fixtures]" >&2
  exit 2
  ;;
esac
