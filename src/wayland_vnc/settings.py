"""State and actions behind the GTK settings app, with no GTK or host coupling.

Every option the UI offers must map to a real effect in `runtime` or the installed
systemd --user unit; this module is that mapping. It deliberately contains no
placeholder switches: an option is either backed by a working action here, or it is
reported unavailable with the reason, so the view can disable it honestly.

All process and filesystem boundaries are injectable, so the logic is unit-tested
without a compositor, WayVNC, systemd, or OpenSSL.
"""

import ipaddress
import json
import os
import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from wayland_vnc import runtime
from wayland_vnc.i18n import _, translatable
from wayland_vnc.probe import collect, report, select_backend

UNIT = "wayland-vnc.service"
# Interfaces a device on the Wi-Fi/LAN cannot reach: container and VM bridges, veth
# pairs, and loopback. Anything else (wl*, en*, eth*, wwan, tun...) is shown.
VIRTUAL_PREFIXES = ("lo", "docker", "virbr", "lxc", "lxd", "br-", "veth", "vnet", "tap")
# What each probed binary is for, so a missing one reads as a fact rather than a fault.
# Only the backend this desktop actually selected needs its server binary present.
# Values are marked here and translated where they are shown: this table is built
# once at import time, while the language can change while the window is open.
BINARY_PURPOSE = {
    "wayvnc": translatable("WayVNC server, for wlroots compositors"),
    "w0vncserver": translatable("TigerVNC server, for KDE Plasma"),
    "gnome-remote-desktop-daemon": translatable("GNOME Remote Desktop server"),
    "gdbus": translatable("used to probe portals over D-Bus"),
    "wayland-info": translatable("used to probe Wayland protocols"),
    "vncviewer": translatable("RealVNC Viewer, a client; not needed to serve"),
}
# The server binary each backend needs; anything else missing is irrelevant here.
BACKEND_BINARY = {
    "wayvnc": "wayvnc",
    "w0vncserver": "w0vncserver",
    "grd": "gnome-remote-desktop-daemon",
}
PROJECT_URL = "https://github.com/ventura8/wayland-vnc"
ISSUE_URL = f"{PROJECT_URL}/issues"
LICENSE_NAME = "GPL-2.0-or-later"
LOOPBACK = ("127.0.0.1", "::1", "localhost")
ANY = ("0.0.0.0", "::")


def product_version() -> str:
    """The installed version, read from the VERSION the packages ship.

    Resolved relative to this module so it works from any prefix -- /usr, $SNAP/usr,
    /app or an AppDir -- and falls back to the repository's own VERSION when running
    from a source checkout. An unreadable file yields "unknown" rather than stopping
    the window from opening over something cosmetic.
    """
    module = Path(__file__).resolve()
    for parent in module.parents:
        candidate = parent / "share" / "wayland-vnc" / "VERSION"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip() or "unknown"
    checkout = module.parent.parent.parent / "VERSION"
    try:
        return checkout.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _systemctl(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Query systemd --user, tolerating hosts that have no systemctl at all.

    Not every supported platform runs systemd, and a container may have none. Report
    the unit as absent in that case instead of letting the app die on OSError.
    """
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 1, "", "not-found")


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _command(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a plain command (no systemctl prefix); a missing binary reads as failure."""
    try:
        return subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 1, "", "not-found")


@dataclass(frozen=True)
class ServiceState:
    """What systemd --user reports about the installed unit."""

    installed: bool
    enabled: bool
    active: bool
    detail: str


@dataclass(frozen=True)
class CredentialState:
    """Whether a viewer credential exists, and whether its mode is still restrictive."""

    present: bool
    mode: str | None

    @property
    def secure_mode(self) -> bool:
        return self.mode == "600"


@dataclass(frozen=True)
class ServerConfig:
    """The provisioned WayVNC configuration as it is actually written on disk."""

    present: bool
    address: str | None
    port: int | None
    auth_enabled: bool

    @property
    def scope(self) -> str:
        """Who can reach the bind: 'loopback', 'any-network', 'local-network' or 'public'.

        A wildcard bind is 'any-network': every network this machine is on, since a
        systemd user unit cannot fence traffic. One private or link-local address is
        'local-network' (that interface's network); anything else is 'public'.
        """
        if self.address in LOOPBACK:
            return "loopback"
        if self.address in ANY:
            return "any-network"
        try:
            parsed = ipaddress.ip_address(self.address or "")
        except ValueError:
            return "public"
        return "local-network" if parsed.is_private or parsed.is_link_local else "public"

    @property
    def loopback_only(self) -> bool:
        return self.scope == "loopback"

    @property
    def lan_access(self) -> bool:
        """The state of the settings app's switch: on for a wildcard bind."""
        return self.scope == "any-network"


@dataclass(frozen=True)
class Status:
    """Everything the status view renders, gathered from real sources only."""

    backend_candidate: str | None
    reason: str
    session_type: str
    credential: CredentialState
    config: ServerConfig
    service: ServiceState
    diagnostic: dict = field(repr=False)

    @property
    def wayland(self) -> bool:
        return self.session_type == "wayland"


@dataclass(frozen=True)
class ConnectInfo:
    """What to type into a viewer on another device: name/addresses plus the port."""

    hostname: str
    addresses: tuple[tuple[str, str], ...]  # (interface, IPv4)
    port: int

    @property
    def targets(self) -> list[tuple[str, str]]:
        """(label, host:port) pairs, easiest option first."""
        rows = [(_("mDNS name"), f"{self.hostname}.local:{self.port}")]
        rows += [(iface, f"{ip}:{self.port}") for iface, ip in self.addresses]
        return rows


def _is_virtual(ifname: str) -> bool:
    return any(ifname.startswith(prefix) for prefix in VIRTUAL_PREFIXES)


def local_addresses(run: Runner | None = None) -> tuple[tuple[str, str], ...]:
    """IPv4 addresses a device on the local network can reach, via `ip -json addr`.

    iproute2 is on every supported platform; when it is missing or the output is not
    JSON, no addresses are reported rather than guessed.
    """
    # Runner defaults are resolved at call time throughout this module, never bound
    # into the signature: a test guard that replaces _command or _systemctl must
    # catch callers that passed nothing.
    run = _command if run is None else run
    result = run(["ip", "-json", "-4", "addr", "show"])
    if result.returncode != 0:
        return ()
    try:
        interfaces = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ()
    found = []
    for interface in interfaces:
        name = interface.get("ifname", "")
        if _is_virtual(name):
            continue
        for info in interface.get("addr_info", []):
            if info.get("family") == "inet" and info.get("scope") == "global" and info.get("local"):
                found.append((name, info["local"]))
    return tuple(found)


def connect_info(
    config: ServerConfig, *, run: Runner | None = None, hostname: str | None = None
) -> ConnectInfo:
    """Resolve the name, addresses and port a viewer needs; port falls back to 5900."""
    return ConnectInfo(
        hostname=socket.gethostname() if hostname is None else hostname,
        addresses=local_addresses(_command if run is None else run),
        port=config.port or runtime.DEFAULT_PORT,
    )


@dataclass(frozen=True)
class _Effective:
    """A credential the backend enforces that our own validation would reject.

    GNOME Remote Desktop accepts passwords outside the 6-64 ASCII rule `Credentials`
    imposes. The dialog must still show what actually works rather than hide it.
    """

    username: str
    password: str


@dataclass(frozen=True)
class Option:
    """A control the view may render. Disabled options carry the reason, never silence."""

    key: str
    label: str
    available: bool
    reason: str


def _mode_of(path: Path) -> str | None:
    if not path.exists():
        return None
    return format(path.stat().st_mode & 0o777, "03o")


def _read_config(directory: Path) -> dict[str, str]:
    path = directory / runtime.CONFIG_NAME
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def service_state(run: Runner | None = None) -> ServiceState:
    """Ask systemd --user about the unit; absence is reported, never guessed."""
    run = _systemctl if run is None else run
    shown = run(["is-enabled", UNIT])
    word = shown.stdout.strip() or shown.stderr.strip()
    if "not-found" in word or "No such file" in word:
        return ServiceState(False, False, False, translatable("unit is not installed"))
    active = run(["is-active", UNIT]).stdout.strip()
    return ServiceState(
        installed=True,
        enabled=word in ("enabled", "enabled-runtime", "static"),
        active=active == "active",
        detail=f"{word}, {active or 'inactive'}",
    )


def gather(
    directory: Path | None = None,
    *,
    capabilities: dict | None = None,
    run: Runner | None = None,
) -> Status:
    """Collect real status: capability verdict, credential, config, and unit state."""
    run = _systemctl if run is None else run
    directory = runtime.config_dir() if directory is None else directory
    diagnostic = report(collect() if capabilities is None else capabilities)
    values = _read_config(directory)
    port = values.get("port")
    credentials = directory / runtime.CREDENTIALS_NAME
    return Status(
        backend_candidate=diagnostic["backend_candidate"],
        reason=diagnostic["reason"],
        session_type=diagnostic["capabilities"].get("session_type", "unknown"),
        credential=CredentialState(credentials.exists(), _mode_of(credentials)),
        config=ServerConfig(
            present=bool(values),
            address=values.get("address"),
            port=int(port) if port and port.isdigit() else None,
            auth_enabled=values.get("enable_auth") == "true",
        ),
        service=service_state(run),
        diagnostic=diagnostic,
    )


@dataclass(frozen=True)
class DiagnosticSection:
    """One collapsible category of the diagnostic report."""

    key: str
    title: str
    summary: str
    rows: tuple[tuple[str, str], ...]


def _yes_no(value: object) -> str:
    return _("Yes") if value else _("No")


def _known(value: str | None) -> str:
    """A probed value as the window shows it: the value itself, or a translated
    "unknown" when the probe could not say (never the English fallback literal)."""
    return value if value and value != "unknown" else _("unknown")


def _binary_detail(name: str, path: str | None) -> str:
    """Where the binary is, or that it is absent -- always with what it is for."""
    purpose = BINARY_PURPOSE.get(name)
    where = path or _("Not installed")
    return f"{where} — {_(purpose)}" if purpose else where


def _binary_summary(binaries: dict, backend: str | None) -> str:
    """Say whether anything that MATTERS is missing, not just how many were found.

    A bare count reads as a fault. Most probed binaries are other desktops' servers or
    probe helpers, so their absence is normal; only the selected backend's own server
    is required here.
    """
    if not binaries:
        return _("None checked")
    missing = sorted(name for name, path in binaries.items() if not path)
    if not missing:
        return _("All present")
    required = BACKEND_BINARY.get(backend or "")
    # Only meaningful if the probe actually looks for it: GNOME Remote Desktop, for
    # instance, is detected over D-Bus and its daemon is never probed by name, so a
    # lookup miss there says nothing about whether it is installed.
    if required in binaries and not binaries[required]:
        return _("%s is MISSING and this desktop needs it") % required
    return _("Everything this desktop needs; not installed: ") + ", ".join(missing)


def diagnostic_sections(diagnostic: dict) -> list[DiagnosticSection]:
    """Group the raw diagnostic into categories a person can actually read.

    The report is a flat JSON blob; this turns it into titled groups with a one-line
    summary each, so the dialog can render them collapsed and let the reader open only
    what they care about. Nothing is invented: every row comes from the report.
    """
    capabilities = diagnostic.get("capabilities", {})
    interfaces = capabilities.get("interfaces", [])
    binaries = capabilities.get("binaries", {})
    portals = (
        (_("RemoteDesktop Portal"), capabilities.get("remote_desktop_portal")),
        (_("ScreenCast Portal"), capabilities.get("screencast_portal")),
    )
    services = (
        ("GNOME Remote Desktop", capabilities.get("gnome_remote_desktop")),
        (_("GNOME Screencast"), capabilities.get("gnome_screencast")),
        ("KWin", capabilities.get("kwin")),
    )
    return [
        DiagnosticSection(
            "verdict",
            _("Verdict"),
            diagnostic.get("backend_candidate") or _("No supported backend"),
            (
                (_("Backend Candidate"), diagnostic.get("backend_candidate") or _("None")),
                (_("Reason"), _(diagnostic.get("reason", ""))),
                (_("Ready to Install"), _yes_no(diagnostic.get("ready_to_install"))),
            ),
        ),
        DiagnosticSection(
            "session",
            _("Session"),
            _known(capabilities.get("session_type")),
            (
                (_("Session Type"), _known(capabilities.get("session_type"))),
                (_("Desktop"), _known(capabilities.get("desktop_hint"))),
            ),
        ),
        DiagnosticSection(
            "protocols",
            _("Wayland Protocols"),
            _("%d capture/input interface(s)") % len(interfaces)
            if interfaces
            else _("None advertised"),
            tuple((name, _("Advertised")) for name in interfaces)
            or ((_("None"), _("Not advertised")),),
        ),
        DiagnosticSection(
            "portals",
            _("Portals"),
            ", ".join(name for name, present in portals if present) or _("None available"),
            tuple((name, _yes_no(present)) for name, present in portals),
        ),
        DiagnosticSection(
            "services",
            _("Desktop Services"),
            ", ".join(name for name, present in services if present) or _("None detected"),
            tuple((name, _yes_no(present)) for name, present in services),
        ),
        DiagnosticSection(
            "binaries",
            _("Binaries"),
            _binary_summary(binaries, diagnostic.get("backend_candidate")),
            tuple((name, _binary_detail(name, path)) for name, path in sorted(binaries.items()))
            or ((_("None"), _("Not checked")),),
        ),
        DiagnosticSection(
            "targets",
            _("Release Targets"),
            _("%d desktops") % len(diagnostic.get("release_targets", [])),
            tuple((name, _("Targeted")) for name in diagnostic.get("release_targets", [])),
        ),
    ]


class Actions:
    """The real, side-effecting operations the settings app exposes.

    Nothing here is cosmetic: each method either changes stored configuration through
    `runtime`, or drives the installed systemd --user unit.
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        run: Runner | None = None,
        generate_key: Callable[[Path], None] = runtime.default_key_generator,
        capabilities: dict | None = None,
        host: runtime.Host | None = None,
    ):
        self.directory = runtime.config_dir() if directory is None else directory
        self.run = _systemctl if run is None else run
        self.generate_key = generate_key
        self.capabilities = capabilities
        # What the last status() probed, so opening a dialog does not run the whole
        # capability probe a second time (D-Bus calls and wayland-info) before the
        # window can draw. Injected capabilities always win over it.
        self._probed: dict | None = None
        # The same OS boundary `serve` uses, so a GNOME host's password sync goes
        # through grdctl for real and through a fake in tests.
        self.host = runtime.Host(shutil.which, os.execv) if host is None else host

    def _capabilities(self) -> dict:
        if self.capabilities is not None:
            return self.capabilities
        if self._probed is None:
            self._probed = collect()
        return self._probed

    def status(self) -> Status:
        return gather(self.directory, capabilities=self._capabilities(), run=self.run)

    def credential(self):
        """The credential the SERVER will actually accept, or None if unprovisioned.

        On a GNOME host the authority is GNOME Remote Desktop's own keyring entry, not
        our file: `serve` adopts an already-enabled grd rather than overwriting the
        password someone set in GNOME Settings, so the two legitimately differ. Showing
        our copy there would hand the user a password the server rejects -- which is
        exactly what happened in use. Prefer the backend's value and fall back to ours.
        """
        try:
            stored = runtime.read_credentials(self.directory)
        except (OSError, ValueError):
            # A truncated or unreadable credential file should not break the dialog;
            # the user can simply set a new password.
            stored = None
        backend, _reason = select_backend(self._capabilities())
        if backend == "grd":
            effective = runtime.grd_password()
            if effective:
                username = stored.username if stored else "vnc"
                try:
                    return runtime.Credentials(username, effective)
                except ValueError:
                    # grd allows passwords our own rules would reject; still show it.
                    return _Effective(username, effective)
        return stored

    def set_credential(self, username: str, password: str) -> Path:
        """Store the viewer credential (mode 600) through the same path the CLI uses.

        On a GNOME host the password also goes into GNOME Remote Desktop, which keeps
        its own copy in the keyring; WayVNC reads our file directly.
        """
        # The cached probe: without it each helper here ran collect() again (D-Bus
        # calls and wayland-info) although status() had just done so.
        capabilities = self._capabilities()
        runtime.check_password_for_backend(password, capabilities)
        path = runtime.set_password(
            self.directory, read_secret=lambda _prompt: password, username=username
        )
        synced = runtime.sync_backend_password(
            runtime.read_credentials(self.directory),
            which=self.host.which,
            run=self.host.run,
            capabilities=capabilities,
        )
        if synced is None:
            # WayVNC reads the password out of the config file, so it has to be
            # rewritten and the server restarted; otherwise the window would report the
            # new password while the server kept accepting the old one.
            if runtime.refresh_config(self.directory, generate_key=self.generate_key):
                runtime.restart_service(which=self.host.which, run=self.host.run)
        return path

    def apply_network(self, address: str, port: int) -> Path:
        """Store the bind with the CLI's validation, and make the server use it now.

        Stored first, applied second: WayVNC reads the config at start, so our unit is
        restarted if it runs; GNOME Remote Desktop gets the address through its unit
        drop-in and is restarted when that changed. Without the second step the window
        would report a bind the server was not yet listening on.
        """
        path = runtime.provision(
            self.directory, address=address, port=port, generate_key=self.generate_key
        )
        runtime.apply_bind(
            self.directory,
            which=self.host.which,
            run=self.host.run,
            capabilities=self._capabilities(),
            private_grd=self.host.private_grd,
        )
        return path

    def set_lan_access(self, enabled: bool) -> Path:
        """The one-switch form of apply_network: every network this computer is on,
        or this computer only, on whichever port is already configured."""
        _address, port = runtime.bind_address(self.directory)
        address = runtime.LAN_ADDRESS if enabled else runtime.LOOPBACK_ADDRESS
        return self.apply_network(address, port)

    def lan_access_option(self, status: Status) -> Option:
        """The local-network switch: real on WayVNC and on the private GNOME daemon,
        otherwise insensitive with the reason, never a switch that does nothing."""
        backend = status.backend_candidate
        label = _("Local Network Access")
        if backend == "wayvnc" or (backend == "grd" and self.host.private_grd()):
            return Option("lan", label, True, "")
        if backend == "grd":
            reason = _(
                "GNOME Remote Desktop's own daemon listens on every network; install "
                "wayland-vnc-grd to control this"
            )
        elif backend == "w0vncserver":
            reason = _("This desktop's backend always listens on this computer only")
        else:
            reason = _("No supported backend")
        return Option("lan", label, False, reason)

    def set_enabled(self, enabled: bool) -> str:
        verb = "enable" if enabled else "disable"
        return self._unit(verb, f"service {verb}d")

    def set_active(self, active: bool) -> str:
        verb = "start" if active else "stop"
        return self._unit(verb, "service started" if active else "service stopped")

    def _unit(self, verb: str, message: str) -> str:
        result = self.run([verb, UNIT])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"systemctl {verb} failed")
        return message

    def options(self, status: Status) -> list[Option]:
        """Describe which controls are genuinely usable now, and why not otherwise."""
        return [
            Option("credential", _("Set Viewer Password"), True, ""),
            Option(
                "network",
                _("Bind Address and Port"),
                status.credential.present,
                "" if status.credential.present else _("Set a viewer password first"),
            ),
            Option("diagnostic", _("Diagnostics"), True, ""),
        ]

    def service_reason(self, status: Status) -> str:
        """Why the Service switches are insensitive, or "" when they are usable."""
        return self._service_reason(status)

    @staticmethod
    def _service_reason(status: Status) -> str:
        if not status.service.installed:
            return _("Install the wayland-vnc package to get the systemd user unit")
        if not status.credential.present:
            return _("Set a viewer password first")
        if not status.wayland:
            return _(status.reason)
        return ""
