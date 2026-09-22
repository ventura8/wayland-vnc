#!/usr/bin/env bash
# Analyse this working tree with SonarQube Cloud and print the dashboard URL.
#
# The same sonar-project.properties the CI job uses drives this run, so a finding
# reported here is the finding CI reports (AGENTS.md, "Local pipeline & CI parity").
# Coverage is produced by the project's own pinned pytest/coverage from the venv --
# never a system interpreter -- and handed to the analyser as coverage.xml.
#
# The token is read from the environment and never written to the repository, never
# passed on a command line where `ps` would show it, and never echoed:
#
#   read -rs SONAR_TOKEN && export SONAR_TOKEN   # paste, no shell history
#   scripts/run-sonar-scan.sh                    # analyse the current branch
#   scripts/run-sonar-scan.sh --no-coverage      # skip the test run, reuse coverage.xml
#
# SONAR_HOST_URL defaults to SonarQube Cloud; point it at a self-hosted server to
# analyse against that instead.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

# Pinned by digest like every other tool image the project runs, so an upstream retag
# cannot change what analysed the code.
SCANNER_IMAGE="sonarsource/sonar-scanner-cli@sha256:a3f4215076706c95a17a68c19322ee916e40a3acd081a8c1a1e839e0194afa57"
host_url=${SONAR_HOST_URL:-https://sonarcloud.io}
with_coverage=1

case "${1:-}" in
"") ;;
--no-coverage) with_coverage=0 ;;
*)
  echo "Usage: $0 [--no-coverage]" >&2
  exit 2
  ;;
esac

if [[ -z "${SONAR_TOKEN:-}" ]]; then
  echo "SONAR_TOKEN is not set: generate a user token at ${host_url%/}/account/security" \
    "and export it before running this script" >&2
  exit 2
fi

if ((with_coverage)); then
  PATH="$(bash scripts/ensure-venv.sh):$PATH"
  export PATH PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
  python3 -m coverage run -m pytest
  # Sonar reads Cobertura XML; the per-file gate reads the JSON. Both come from this
  # one run so the two can never disagree about what was covered.
  python3 -m coverage xml -o coverage.xml
fi

version=$(cat VERSION)
branch=$(git rev-parse --abbrev-ref HEAD)
# SonarQube Cloud's free plan analyses the main branch and pull requests; a long-lived
# feature branch is a paid feature, and naming one here produces a branch entry the
# server never analyses -- a scan that looks like it worked and reports nothing. A
# local run is a pre-merge check of the working tree, so it is sent as the main branch
# unless WAYLAND_VNC_SONAR_BRANCH names another, and says which it used.
main_branch=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
main_branch=${main_branch#origin/}
analysed=${WAYLAND_VNC_SONAR_BRANCH:-${main_branch:-main}}
if [[ "$analysed" != "$branch" ]]; then
  echo "note: on '$branch'; analysing as '$analysed' (the free plan has no feature-branch" \
    "analysis). Set WAYLAND_VNC_SONAR_BRANCH to override." >&2
fi

# The analyser needs the network to reach SonarQube Cloud, so --network none is not
# available here; everything else stays as locked down as the offline linters, and the
# token arrives through the environment rather than the process table.
docker run --rm \
  --cap-drop ALL --security-opt no-new-privileges \
  -e SONAR_TOKEN -e SONAR_HOST_URL="$host_url" \
  -v "$PWD:/usr/src" -w /usr/src \
  "$SCANNER_IMAGE" \
  -Dsonar.projectVersion="$version" \
  -Dsonar.branch.name="$analysed"
