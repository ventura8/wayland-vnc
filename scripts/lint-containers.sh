#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Immutable tool images, read-only source, no network or host desktop access.
run_linter() {
  docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
    -v "$PWD:/work:ro" -w /work "$@"
}
run_linter davidanson/markdownlint-cli2@sha256:839558fd0d36c46da0e01ea84fd1d20a2822b5a8a60c16dc9708f0bb7c9e903b \
  '**/*.md' '#vendor' '#artifacts'
# The config names the runner labels actionlint's bundled list lacks. Passed by path:
# actionlint only finds it on its own next to a .git directory, and a tree synced to
# a lab machine or unpacked from an archive has none.
run_linter rhysd/actionlint@sha256:887a259a5a534f3c4f36cb02dca341673c6089431057242cdc931e9f133147e9 \
  -config-file .github/actionlint.yaml .github/workflows/*.yml
run_linter hadolint/hadolint@sha256:30a8fd2e785ab6176eed53f74769e04f125afb2f74a6c52aef7d463583b6d45e \
  hadolint docker/Dockerfile.checks docker/Dockerfile.sway docker/Dockerfile.labwc \
  docker/Dockerfile.xfce-labwc docker/Dockerfile.lxqt-labwc docker/Dockerfile.wayfire \
  docker/Dockerfile.hyprland docker/Dockerfile.plasma docker/Dockerfile.gnome docker/Dockerfile.viewer-harness docker/Dockerfile.android
