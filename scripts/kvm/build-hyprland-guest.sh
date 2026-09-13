#!/usr/bin/env bash
# Kept for the documented name: the Hyprland guest is one target of the generic
# builder now (scripts/kvm/build-guest.sh), which gives every target its own work
# directory (artifacts/kvm/<target>) and forwarded port.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
exec bash scripts/kvm/build-guest.sh hyprland "$@"
