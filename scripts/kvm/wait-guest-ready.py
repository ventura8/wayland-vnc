#!/usr/bin/env python3
"""Wait until a KVM guest is provisioned and its fixture is up, through the guest agent.

The guest signals readiness by creating /run/wayland-vnc-guest-ready once cloud-init
has finished and the fixture unit has started. This is used instead of watching the
serial console: a desktop guest reboots once (to load its custom EDID), so the console
"Cloud-init finished" line appears on the first boot, before the fixture exists.

    scripts/kvm/wait-guest-ready.py <work-dir> <timeout-seconds>
"""

import sys
import time
from pathlib import Path

from wayland_vnc.qemu_guest import QemuGuest


def main() -> int:
    work, timeout = Path(sys.argv[1]), int(sys.argv[2])
    guest = QemuGuest(work)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # A desktop guest reboots once to load its EDID; an agent that answered the
        # ping can go away mid-request, which is a reason to ask again, not to fail.
        try:
            if guest.ping(2):
                ready = guest.shell("test -f /run/wayland-vnc-guest-ready", timeout=15)
                if ready.exitcode == 0:
                    print("ready")
                    return 0
        except OSError:
            pass
        time.sleep(10)
    print(f"the guest under {work} was not ready within {timeout}s", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
