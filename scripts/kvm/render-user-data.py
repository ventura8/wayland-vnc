#!/usr/bin/env python3
"""Render a KVM guest's cloud-init user-data from its template.

Called by scripts/kvm/build-guest.sh with the template and output paths on the
command line and everything else in the environment, never on argv:

  WAYLAND_VNC_TEMPLATE_TARGET    the qualification target (also names the Dockerfile
                                 whose package list the guest installs)
  WAYLAND_VNC_TEMPLATE_HASH      the fixture account's password hash (openssl passwd -6)
  WAYLAND_VNC_TEMPLATE_PASSWORD  the fixture's VNC password (one line)
  WAYLAND_VNC_TEMPLATE_KEY       path of the private server key to pin, or empty

Placeholders are replaced literally (no shell or sed quoting can corrupt a password);
multi-line values are indented into the template's block scalars line by line. The
package list is read from docker/Dockerfile.<target> so the guest runs exactly the
distribution packages the container fixture qualified with.
"""

import os
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
# Packages the container needs that mean nothing in a virtual machine.
CONTAINER_ONLY = {"dbus-daemon"}


# Per-target session environment the container image sets with ENV and the guest unit
# must set too (the wlroots targets need nothing beyond the template's own lines).
DESKTOP_ENVIRONMENT = {
    "gnome": [
        "XDG_CURRENT_DESKTOP=GNOME",
        "GSETTINGS_BACKEND=keyfile",
        "GSETTINGS_SCHEMA_DIR=/opt/wayland-vnc/grd/share/glib-2.0/schemas",
        "LANG=C.UTF-8",
    ],
    "plasma": [
        "XDG_CURRENT_DESKTOP=KDE",
        "XDG_MENU_PREFIX=plasma-",
        "XDG_DATA_DIRS=/usr/local/share:/usr/share",
        "QT_QPA_PLATFORM=wayland",
        "QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1",
        "LANG=C.UTF-8",
    ],
}
SESSION_SCRIPTS = {"gnome": "gnome-session.py", "plasma": "plasma-session.py"}


def dockerfile_packages(target: str) -> list[str]:
    """The apt packages the target's container image installs, in order: the last
    install block, which is the runtime stage (the GNOME and Plasma images build
    their VNC servers in earlier stages with packages the guest does not need)."""
    text = (REPO / "docker" / f"Dockerfile.{target}").read_text(encoding="utf-8")
    blocks = re.findall(r"apt-get install -y --no-install-recommends(.*?)&&", text, re.S)
    if not blocks:
        raise SystemExit(f"no apt-get install line in docker/Dockerfile.{target}")
    packages = [token for token in blocks[-1].replace("\\", " ").split() if token]
    return [package for package in packages if package not in CONTAINER_ONLY]


def block(lines: list[str], indent: str) -> str:
    return "\n".join(indent + line if line else "" for line in lines)


def main() -> None:
    template_path, out_path = (pathlib.Path(arg) for arg in sys.argv[1:3])
    env = os.environ
    target = env["WAYLAND_VNC_TEMPLATE_TARGET"]
    template = template_path.read_text(encoding="utf-8")
    values = {
        "__TARGET__": target,
        "__PASSWORD_HASH__": env["WAYLAND_VNC_TEMPLATE_HASH"],
        "__VNC_PASSWORD__": env["WAYLAND_VNC_TEMPLATE_PASSWORD"],
    }
    for name in ("__PASSWORD_HASH__", "__VNC_PASSWORD__"):
        if "\n" in values[name] or "\r" in values[name]:
            raise SystemExit(f"{name} must be a single line")
    if "__PACKAGES__" in template:
        values["__PACKAGES__"] = block([f"- {p}" for p in dockerfile_packages(target)], "  ")
    if "__HEADLESS_OUTPUTS__" in template:
        # sway and Hyprland create their hot-plug output on request; the others keep
        # one spare headless output switched off.
        values["__HEADLESS_OUTPUTS__"] = "0" if target in ("sway", "hyprland") else "1"
    if "__DESKTOP_ENVIRONMENT__" in template:
        lines = [f"Environment={item}" for item in DESKTOP_ENVIRONMENT[target]]
        values["__DESKTOP_ENVIRONMENT__"] = block(lines, "      ")
    if "__SESSION_SCRIPT__" in template:
        values["__SESSION_SCRIPT__"] = SESSION_SCRIPTS[target]
    if "__EDID_B64__" in template:
        values["__EDID_B64__"] = env.get("WAYLAND_VNC_TEMPLATE_EDID_B64", "")
    if "__SERVER_KEY__" in template:
        key_path = env.get("WAYLAND_VNC_TEMPLATE_KEY", "")
        if not key_path:
            raise SystemExit("this template pins a server key: WAYLAND_VNC_TEMPLATE_KEY is empty")
        key = pathlib.Path(key_path).read_text(encoding="utf-8").strip().splitlines()
        values["__SERVER_KEY__"] = block(key, "      ")
    for name, value in values.items():
        if name not in template:
            raise SystemExit(f"{name} is not in {template_path}")
        template = template.replace(name, value)
    leftover = re.findall(r"__[A-Z_]+__", template)
    if leftover:
        raise SystemExit(f"unreplaced placeholders in {template_path}: {sorted(set(leftover))}")
    out_path.write_text(template, encoding="utf-8")


if __name__ == "__main__":
    main()
