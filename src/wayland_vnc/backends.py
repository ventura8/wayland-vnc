"""Backend descriptions and safe, project-scoped service configuration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Backend:
    """Immutable backend contract consumed by packaging and lifecycle code."""

    name: str
    executable: str
    arguments: tuple[str, ...]
    required_binary: str
    requires_portal: bool
    environment: tuple[str, ...] = ()

    def service_unit(self) -> str:
        environment = "".join(f"Environment={value}\n" for value in self.environment)
        command = " ".join((self.executable, *self.arguments))
        return (
            "[Unit]\n"
            f"Description=wayland-vnc {self.name} backend\n"
            "Documentation=https://github.com/ventura8/wayland-vnc\n"
            "After=graphical-session.target\n"
            "PartOf=graphical-session.target\n"
            # Same gate the packaged unit uses: no Wayland display, no server.
            "ConditionEnvironment=WAYLAND_DISPLAY\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"{environment}"
            f"ExecStart={command}\n"
            "Restart=on-failure\n"
            "RestartSec=2s\n"
            # Deliberately no mount sandbox (ProtectSystem, ProtectHome,
            # ReadWritePaths, PrivateTmp) and no IPAddressAllow: in a user manager
            # the former create an unprivileged user namespace that Ubuntu's
            # AppArmor bars from reading other processes' fd tables (which serve on
            # GNOME needs) without applying the read-only mounts, and the latter is
            # not applied at all. See packaging/systemd/wayland-vnc.service.
            "NoNewPrivileges=yes\n\n"
            "[Install]\n"
            "WantedBy=graphical-session.target\n"
        )


BACKENDS = {
    "grd": Backend(
        name="grd",
        executable="/opt/wayland-vnc/grd/libexec/gnome-remote-desktop-daemon",
        arguments=("--vnc-port", "5900"),
        required_binary="gnome-remote-desktop-daemon",
        requires_portal=False,
        environment=("WAYLAND_VNC_ENABLE_DMABUF=0", "WAYLAND_VNC_ENABLE_CLIPBOARD=0"),
    ),
    # RealVNC Viewer negotiates RA2_256/RA2 with TigerVNC; the fixture evidence used
    # exactly these security types with a password file and no username requirement.
    "w0vncserver": Backend(
        name="w0vncserver",
        executable="/opt/wayland-vnc/tigervnc/bin/w0vncserver",
        arguments=(
            "-localhost",
            "-rfbport=5900",
            "-SecurityTypes=RA2_256,RA2",
            "-RSAKey=%h/.config/wayland-vnc/rsa.pem",
            "-RequireUsername=0",
            "-RememberDisplayChoice=Always",
            "-PasswordFile=%h/.config/wayland-vnc/passwd",
        ),
        required_binary="w0vncserver",
        requires_portal=True,
    ),
    "wayvnc": Backend(
        name="wayvnc",
        executable="/opt/wayland-vnc/wayvnc/bin/wayvnc",
        arguments=("--config=%h/.config/wayland-vnc/wayvnc.conf",),
        required_binary="wayvnc",
        requires_portal=False,
    ),
}


def get_backend(name: str) -> Backend:
    try:
        return BACKENDS[name]
    except KeyError as error:
        raise ValueError(f"Unknown backend: {name}") from error
