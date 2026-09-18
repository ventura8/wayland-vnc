#!/usr/bin/env bash
# Write a private RealVNC .vnc connection file for one loopback fixture port.
# The password is read from the private fixture.conf, obfuscated with RealVNC's
# own key by wayland_vnc.hardware (pinned against a file the actual viewer
# accepted), and never printed or passed on argv.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Python here runs from the project venv (AGENTS.md); ensure-venv.sh is idempotent.
PATH="$(bash scripts/ensure-venv.sh):$PATH"
export PATH
umask 077

usage() {
  echo "Usage: $0 --credential FILE --port N --output FILE.vnc" >&2
}

credential=
port=
output=
while (($#)); do
  case "$1" in
  --credential)
    credential=${2:-}
    shift 2
    ;;
  --port)
    port=${2:-}
    shift 2
    ;;
  --output)
    output=${2:-}
    shift 2
    ;;
  *)
    usage
    exit 2
    ;;
  esac
done
if [[ -z "$credential" || -z "$port" || -z "$output" || ! -f "$credential" ]]; then
  usage
  exit 2
fi
if [[ $(stat -c '%a' -- "$credential") != 600 ]]; then
  echo "Refusing a credential file whose mode is not exactly 600." >&2
  exit 2
fi
if [[ "$output" != *.vnc ]] || ! git check-ignore --no-index --quiet -- "$output"; then
  echo "The output must be a Git-ignored .vnc file." >&2
  exit 2
fi
username=$(sed -n 's/^username=//p' "$credential" | head -1)
if [[ "$username" != fixture ]]; then
  echo "Credential file is not the disposable fixture account." >&2
  exit 2
fi
# An empty password would obfuscate to a perfectly well-formed 16-character value
# (DES of eight zero bytes) and pass the check below; refuse it here, without ever
# holding the password in a variable.
if ! grep -qE '^password=.+' -- "$credential"; then
  echo "Credential file has no password." >&2
  exit 2
fi
# RealVNC's .vnc Password field uses the classic VNC key with DES's per-byte bit
# reversal, which is NOT what TigerVNC's vncpasswd writes for a server-side password
# file: every connection file built that way was silently refused by the real viewer.
# The password goes in on stdin, never on a command line.
obfuscated=$(sed -n 's/^password=//p' "$credential" | head -1 |
  PYTHONPATH=src python3 -c '
import sys
from wayland_vnc.hardware import obfuscate_password

print(obfuscate_password(sys.stdin.readline().rstrip("\n")))
')
if [[ ${#obfuscated} -ne 16 ]]; then
  echo "Password obfuscation failed." >&2
  exit 1
fi
printf '[Connection]\nHost=127.0.0.1:%s\nUserName=%s\nUsername=%s\nPassword=%s\n\n' \
  "$port" "$username" "$username" "$obfuscated" >"$output"
printf '[Options]\nEncryption=Server\nWarnUnencrypted=0\nAutoReconnect=0\nEnableUdpRfb=0\n' >>"$output"
printf 'ProxyTcpRfb=0\nPasswordStoreOffer=0\n' >>"$output"
chmod 600 -- "$output"
echo "wrote $output for 127.0.0.1:$port"
