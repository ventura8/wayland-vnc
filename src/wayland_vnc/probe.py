"""Read-only capability collection. Never changes the running desktop."""

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping

from wayland_vnc.i18n import translatable

TARGETS = ("gnome", "plasma", "xfce-labwc", "lxqt-labwc", "sway", "hyprland", "wayfire")
BINARIES = ("wayland-info", "gdbus", "wayvnc", "w0vncserver", "vncviewer")


def command(args: list[str]) -> str:
    """Bounded, shell-free read-only probe; unavailable is not success."""
    try:
        result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def session_type(environment: Mapping[str, str]) -> str:
    """What kind of session this is, from the evidence a process can actually see.

    XDG_SESSION_TYPE is set by the login session, but inside a systemd user unit it
    is only present if the compositor imported it into the user manager; many do not,
    while WAYLAND_DISPLAY is imported far more reliably (and is what the installed unit
    is gated on). An unset type with a Wayland display is therefore a Wayland session.
    Anything else is reported as found, or "unknown", and is refused downstream.
    """
    declared = environment.get("XDG_SESSION_TYPE")
    # Exported-but-empty is what a user manager gives when the compositor never
    # set it, so treat it as absent rather than as a declared unknown type.
    if not declared and environment.get("WAYLAND_DISPLAY"):
        return "wayland"
    return declared or "unknown"


def collect(
    env: Mapping[str, str] | None = None,
    run: Callable[[list[str]], str] = command,
    which: Callable[[str], str | None] = shutil.which,
) -> dict:
    environment = os.environ if env is None else env
    binaries = {name: which(name) for name in BINARIES}
    protocols = ""
    if session_type(environment) == "wayland" and binaries["wayland-info"]:
        protocols = run([binaries["wayland-info"]])
    interfaces = sorted(set(re.findall(r"interface:\s*'([^']+)'", protocols)))
    bus_names = ""
    portal = ""
    if binaries["gdbus"]:
        bus_names = run(
            [
                binaries["gdbus"],
                "call",
                "--session",
                "--dest",
                "org.freedesktop.DBus",
                "--object-path",
                "/org/freedesktop/DBus",
                "--method",
                "org.freedesktop.DBus.ListNames",
            ]
        )
        # Do not auto-activate a portal just to diagnose: inspect only an existing owner.
        if "'org.freedesktop.portal.Desktop'" in bus_names:
            portal = run(
                [
                    binaries["gdbus"],
                    "introspect",
                    "--session",
                    "--dest",
                    "org.freedesktop.portal.Desktop",
                    "--object-path",
                    "/org/freedesktop/portal/desktop",
                ]
            )
    return {
        "session_type": session_type(environment),
        "desktop_hint": environment.get("XDG_CURRENT_DESKTOP", "unknown"),
        "interfaces": interfaces,
        "gnome_remote_desktop": "'org.gnome.Mutter.RemoteDesktop'" in bus_names,
        "gnome_screencast": "'org.gnome.Mutter.ScreenCast'" in bus_names,
        "kwin": "'org.kde.KWin'" in bus_names,
        "remote_desktop_portal": "interface org.freedesktop.portal.RemoteDesktop" in portal,
        "screencast_portal": "interface org.freedesktop.portal.ScreenCast" in portal,
        "binaries": binaries,
    }


def select_backend(capabilities: dict) -> tuple[str | None, str]:
    if capabilities["session_type"] != "wayland":
        return None, translatable(
            "A native Wayland session is required; X11 fallback is prohibited."
        )
    if capabilities["gnome_remote_desktop"] and capabilities["gnome_screencast"]:
        return "grd", translatable(
            "Mutter capture and remote-input services detected; private build required."
        )
    if capabilities["kwin"]:
        if capabilities["remote_desktop_portal"] and capabilities["screencast_portal"]:
            return "w0vncserver", translatable("KWin and capture/input portal interfaces detected.")
        return None, translatable(
            "KWin requires both ScreenCast and RemoteDesktop portal interfaces."
        )
    interfaces = set(capabilities["interfaces"])
    capture = (
        "zwlr_screencopy_manager_v1" in interfaces
        or {"ext_image_copy_capture_manager_v1", "ext_output_image_capture_source_manager_v1"}
        <= interfaces
    )
    inputs = {"zwlr_virtual_pointer_manager_v1", "zwp_virtual_keyboard_manager_v1"} <= interfaces
    if capture and inputs:
        return "wayvnc", translatable(
            "Capture, virtual keyboard, and virtual pointer protocols detected."
        )
    return None, translatable("Required capture AND input capabilities were not detected.")


def report(capabilities: dict) -> dict:
    backend, reason = select_backend(capabilities)
    return {
        "schema_version": 1,
        "backend_candidate": backend,
        "reason": reason,
        "qualification": "unqualified",
        "ready_to_install": False,
        "capabilities": capabilities,
        "release_targets": list(TARGETS),
    }
