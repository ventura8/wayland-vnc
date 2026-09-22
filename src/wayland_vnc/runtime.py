"""Runtime provisioning and serving for the installed Wayland VNC service.

This is the product's serving path: it provisions a per-user WayVNC configuration
(bound to this machine only unless local network access has been turned on), stores
viewer credentials with restrictive permissions, and execs the real WayVNC server
inside the user's Wayland session. Every filesystem and process boundary is injectable so
the logic is unit-tested without a real compositor, WayVNC, or OpenSSL.
"""

import importlib
import os
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from wayland_vnc.backends import BACKENDS
from wayland_vnc.i18n import translatable
from wayland_vnc.probe import collect, select_backend

CONFIG_DIR_ENV = "WAYLAND_VNC_CONFIG_DIR"
DEFAULT_PORT = 5900
# This machine only, until local network access is turned on. A systemd user unit
# cannot fence traffic (IPAddressAllow needs a privileged manager and is silently
# not applied by a user one), so a wildcard bind is reachable from every network the
# machine is on -- a shared Wi-Fi included. That is an explicit choice, never the
# default.
LOOPBACK_ADDRESS = "127.0.0.1"
DEFAULT_ADDRESS = LOOPBACK_ADDRESS
# The local-network opt-in: every interface. Devices on the same Wi-Fi or LAN (a
# phone running the RealVNC app) can then connect.
LAN_ADDRESS = "0.0.0.0"
LAN_ADDRESSES = (LAN_ADDRESS, "::")
CONFIG_NAME = "wayvnc.conf"
KEY_NAME = "rsa.pem"
CREDENTIALS_NAME = "credentials"
UNIT = "wayland-vnc.service"
GRD_UNIT = "gnome-remote-desktop.service"
GRD_SECRET_SCHEMA = "org.gnome.RemoteDesktop.VncCredentials"
# Classic VNC authentication uses the password as an 8-byte DES key, so anything
# longer is truncated; GNOME Remote Desktop refuses it outright ("Password is too
# long") rather than silently cutting it.
VNC_AUTH_MAX_PASSWORD = 8


def config_dir(env: dict | None = None) -> Path:
    """Resolve the per-user configuration directory (XDG-aware, overridable in tests)."""
    environment = os.environ if env is None else env
    override = environment.get(CONFIG_DIR_ENV)
    if override:
        return Path(override)
    base = environment.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "wayland-vnc"


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str

    def __post_init__(self):
        if not self.username or not self.username.isascii() or not self.username.isprintable():
            raise ValueError(translatable("Username must be printable ASCII"))
        if (
            not 6 <= len(self.password) <= 64
            or not self.password.isascii()
            # Control characters would be written straight into wayvnc.conf and
            # the keyring; leading or trailing space is invisible to whoever
            # types it into the viewer and so can only cause a failed login.
            or not self.password.isprintable()
            or self.password != self.password.strip()
        ):
            raise ValueError(translatable("Password must be 6-64 ASCII characters"))


def write_private(path: Path, body: str) -> Path:
    """Write `body` to `path` with mode 600 from the moment it exists.

    write_text() then chmod() leaves the file readable by anyone for the instant
    between the two calls, and the content is a plaintext VNC password. O_CREAT with
    0o600 never opens that window; an existing file is re-tightened first, because
    the mode of a file that is already there is not affected by O_CREAT's mode.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        # Only while the raw descriptor is still ours: once fdopen has taken it, the
        # handle's context manager closes it, and closing twice would hit whatever
        # descriptor number got recycled in between.
        os.close(fd)
        raise
    with handle:
        handle.write(body)
    return path


def read_credentials(directory: Path) -> Credentials | None:
    path = directory / CREDENTIALS_NAME
    if not path.exists():
        return None
    fields = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"username", "password"}:
            fields[key] = value
    if "username" not in fields or "password" not in fields:
        raise ValueError(translatable("Credentials file is missing a username or password"))
    return Credentials(fields["username"], fields["password"])


def set_password(
    directory: Path,
    *,
    read_secret: Callable[[str], str],
    username: str = "vnc",
) -> Path:
    """Write the viewer credentials file with mode 600; the secret never hits argv."""
    password = read_secret("VNC viewer password: ")
    confirm = read_secret("Confirm password: ")
    if password != confirm:
        raise ValueError(translatable("Passwords did not match"))
    credentials = Credentials(username, password)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = f"username={credentials.username}\npassword={credentials.password}\n"
    return write_private(directory / CREDENTIALS_NAME, body)


def ensure_credentials(directory: Path, *, username: str = "vnc") -> tuple[Credentials, bool]:
    """Return the stored credential, generating a strong random one on first use.

    The service must be running right after install, and it must never serve without
    authentication; a generated secret satisfies both. It is written with mode 600 and
    can be viewed or replaced in the settings app or with `wayland-vnc set-password`.
    Returns (credentials, generated).
    """
    existing = read_credentials(directory)
    if existing is not None:
        return existing, False
    # token_urlsafe(12) -> 16 URL-safe ASCII characters, inside the 6-64 rule.
    credentials = Credentials(username, secrets.token_urlsafe(12))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(
        directory / CREDENTIALS_NAME,
        f"username={credentials.username}\npassword={credentials.password}\n",
    )
    return credentials, True


def provision(
    directory: Path,
    *,
    address: str = DEFAULT_ADDRESS,
    port: int = DEFAULT_PORT,
    generate_key: Callable[[Path], None],
) -> Path:
    """Create the WayVNC config and RSA key, mode 600, with authentication enabled.

    The default address is loopback: only this machine can reach the server. Pass
    LAN_ADDRESS (what the settings app's local-network switch does) to let a phone or
    another computer on the same Wi-Fi or LAN connect -- and, since nothing fences a
    user service, anything else on any network this machine joins.
    """
    if not 1024 < port <= 65535:
        raise ValueError(translatable("Port must be an unprivileged TCP port"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = directory / KEY_NAME
    if not key_path.exists():
        generate_key(key_path)
        key_path.chmod(0o600)
    credentials = read_credentials(directory)
    lines = [
        f"address={address}",
        f"port={port}",
        "enable_auth=true",
        "relax_encryption=false",
        f"rsa_private_key_file={key_path}",
    ]
    if credentials is not None:
        lines += [f"username={credentials.username}", f"password={credentials.password}"]
    # The config carries the same plaintext password, so it is created private too.
    return write_private(directory / CONFIG_NAME, "\n".join(lines) + "\n")


def read_config(directory: Path) -> tuple[str, int] | None:
    """The address and port already provisioned, or None when there is no usable config."""
    try:
        text = (directory / CONFIG_NAME).read_text(encoding="utf-8")
    except OSError:
        return None
    pairs = (line.partition("=") for line in text.splitlines())
    values = {key: value for key, separator, value in pairs if separator}
    address = values.get("address", "").strip()
    try:
        port = int(values.get("port", "").strip())
    except ValueError:
        return None
    return (address, port) if address else None


def refresh_config(
    directory: Path, *, generate_key: Callable[[Path], None] | None = None
) -> Path | None:
    """Rewrite the WayVNC config so it carries the password that is stored now.

    WayVNC reads the password from this file rather than from the credentials file, so
    storing a new password does not reach the server until the config is rewritten.
    Returns None when nothing is provisioned yet, which is not an error: the next
    `serve` writes the config from the stored credential anyway.

    The address and port already chosen are preserved. Re-provisioning with the
    defaults would silently take a server someone had opened to the local network, or
    moved to another port, back to loopback.
    """
    existing = read_config(directory)
    if existing is None:
        return None
    address, port = existing
    # Resolved here, not as a default argument: the generator is defined below.
    generator = default_key_generator if generate_key is None else generate_key
    return provision(directory, address=address, port=port, generate_key=generator)


def restart_service(*, which: Callable[[str], str | None], run=None) -> bool:
    """Restart our own user unit if it is running, and report whether it was.

    A password change only reaches a running WayVNC by restarting it, which drops any
    session open at that moment -- the same expected cost as on the GNOME backend. A
    unit that is not running needs nothing: it will read the new config when it starts.
    """
    run = _run if run is None else run
    systemctl = which("systemctl")
    if systemctl is None:
        return False
    if run([systemctl, "--user", "is-active", UNIT]).stdout.strip() != "active":
        return False
    return run([systemctl, "--user", "restart", UNIT]).returncode == 0


def default_key_generator(key_path: Path) -> None:  # pragma: no cover - integration path
    subprocess.run(
        ["/usr/bin/openssl", "genrsa", "-traditional", "-out", str(key_path), "2048"],
        check=True,
        timeout=30,
    )


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, check=False, capture_output=True, text=True, timeout=30, input=stdin
    )


GRD_VNC_PORT = 5900
TCP_TABLES = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))


def listener_pid(
    port: int, tables: tuple[Path, ...] = TCP_TABLES, proc: Path = Path("/proc")
) -> int | None:
    """The pid that owns the socket listening on `port`, from the kernel's own tables.

    Deliberately not a connection attempt: gnome-remote-desktop serves one session and
    dies when it ends, so probing it over RFB would consume the server being checked.
    Returns None when nothing listens, and 0 when something listens but its owner is
    not one of our processes (another user's, a session we cannot see into -- or our
    own process reading from inside an unprivileged user namespace, which is why the
    unit that runs this must not use PrivateTmp).
    """
    inodes = set()
    for table in tables:
        try:
            lines = table.read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if (
                len(fields) > 9
                and fields[3] == "0A"
                and int(fields[1].rsplit(":", 1)[1], 16) == port
            ):
                inodes.add(f"socket:[{fields[9]}]")
    if not inodes:
        return None
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            for fd in (entry / "fd").iterdir():
                if os.readlink(fd) in inodes:
                    return int(entry.name)
        except OSError:
            continue
    return 0


def wait_for_listener(
    owner: int,
    port: int = GRD_VNC_PORT,
    *,
    attempts: int = 40,
    delay: float = 0.25,
    listener=listener_pid,
) -> bool:
    """Wait until the socket on `port` belongs to `owner`; a listener of another
    process does not count, it is exactly the impostor this check exists to see."""
    for _ in range(attempts):
        if listener(port) == owner:
            return True
        time.sleep(delay)
    return False


def wait_for_port_free(
    port: int = GRD_VNC_PORT, *, attempts: int = 240, delay: float = 0.25, listener=listener_pid
) -> bool:
    for _ in range(attempts):
        if listener(port) is None:
            return True
        time.sleep(delay)
    return False


def _grd_keyring_password() -> str | None:
    """Default for `Host.grd_password`; the real lookup is defined further down."""
    return grd_password()


def _private_grd_present() -> bool:
    """Default for `Host.private_grd`; the real check is defined further down."""
    return private_grd_installed()


@dataclass(frozen=True)
class Host:
    """How `serve` touches the OS: binary lookup, process replacement, subprocesses.

    One object so every boundary is injectable together; tests hand in fakes and the
    CLI hands in the real thing.
    """

    which: Callable[[str], str | None]
    exec_fn: Callable[[str, list[str]], None]
    # None means "the module's _run", looked up when the Host is built rather than
    # captured when this class was defined: a test guard that replaces runtime._run
    # must catch a Host constructed without an explicit runner too.
    run: Runner | None = field(default=None)
    # Which process listens on a local port, from the kernel's tables; never a connection.
    listener: Callable[[int], int | None] = listener_pid
    # GNOME Remote Desktop's own VNC password (keyring), and where our copy lives.
    grd_password: Callable[[], str | None] = _grd_keyring_password
    # Whether the private GNOME Remote Desktop build is installed: only it can be
    # told where to listen.
    private_grd: Callable[[], bool] = _private_grd_present

    def __post_init__(self):
        if self.run is None:
            object.__setattr__(self, "run", _run)


def _grd_vnc_enabled(gsettings: str, run: Runner) -> bool:
    result = run([gsettings, "get", "org.gnome.desktop.remote-desktop.vnc", "enable"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def grd_password(*, run=None, which: Callable[[str], str | None] | None = None) -> str | None:
    """The password GNOME Remote Desktop will actually accept, or None if unknown.

    grd keeps the VNC password in the login keyring and stores it GVariant-serialised,
    so the raw secret arrives wrapped in single quotes; they are stripped here. This is
    the authority on a GNOME host -- our own credentials file is only a copy, and the
    two can drift if grd is changed elsewhere (GNOME Settings, another tool).
    """
    del run, which  # kept for signature symmetry with the other backend helpers
    try:
        gi = importlib.import_module("gi")
        gi.require_version("Secret", "1")
        secret = importlib.import_module("gi.repository.Secret")
        glib = importlib.import_module("gi.repository.GLib")
    except (ImportError, ValueError):
        return None
    schema = secret.Schema.new(GRD_SECRET_SCHEMA, secret.SchemaFlags.NONE, {})
    try:
        stored = secret.password_lookup_sync(schema, {}, None)
    except glib.Error:
        # A locked keyring, or no Secret Service on the bus, is "unknown", the same
        # answer as no stored secret; it must not take the settings window down.
        return None
    if stored is None:
        return None
    text = stored.strip()
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1]
    return text


def backend_password_limit(capabilities=None) -> int | None:
    """The longest password the active backend will accept, or None if unconstrained.

    GNOME Remote Desktop serves classic VncAuth, whose key is a single 8-byte DES
    block, and rejects anything longer outright. WayVNC reads our credentials file
    and imposes no such limit.
    """
    backend, _reason = select_backend(collect() if capabilities is None else capabilities)
    return VNC_AUTH_MAX_PASSWORD if backend == "grd" else None


def check_password_for_backend(password: str, capabilities=None) -> None:
    """Reject a password the backend cannot accept, before anything is written.

    Without this the password lands in our file, the backend refuses it, and the two
    silently disagree -- the viewer then fails with the password the settings window
    is displaying as current.
    """
    limit = backend_password_limit(capabilities)
    if limit is not None and len(password) > limit:
        # A constant message, so the window can translate it; the limit is the
        # classic VNC authentication's fixed eight characters.
        raise ValueError(
            translatable(
                "This desktop serves VNC through GNOME Remote Desktop, which accepts "
                "passwords of at most 8 characters"
            )
        )


def backend_state(run=None, unit: str = GRD_UNIT) -> str:
    """The unit's activation state as systemd reports it ("active", "failed", ...)."""
    run = _run if run is None else run
    return run(["systemctl", "--user", "is-active", unit]).stdout.strip()


def backend_is_serving(run=None, unit: str = GRD_UNIT) -> bool:
    """Whether the backend daemon is up, asked of systemd rather than of the socket.

    Deliberately does NOT open a VNC connection. gnome-remote-desktop in this build
    serves a single session and segfaults when one ends, so a probe that spoke RFB
    would consume or crash the very server it is checking and hand the next real
    client a refused connection -- the failure this readiness check exists to prevent.
    """
    return backend_state(run, unit) == "active"


def wait_for_backend(
    *,
    run=None,
    settle: float = 1.0,
    attempts: int = 40,
    delay: float = 0.25,
    unit: str = GRD_UNIT,
) -> bool:
    """Block until the backend reports active, recovering it if systemd gave up.

    gnome-remote-desktop segfaults whenever a VNC session ends (upstream defect in the
    distribution's VNC patch). systemd restarts it, but enough crashes inside the
    unit's start-limit window make systemd refuse to start it at all, leaving the
    desktop with no VNC server until someone intervenes. Clearing the limit once and
    starting it again turns that dead end back into a working server.

    Returns whether it came up; a timeout is reported rather than raised, because the
    password has been stored either way.
    """
    run = _run if run is None else run
    recovered = False
    for _ in range(attempts):
        state = backend_state(run, unit)
        if state == "active":
            # systemd calls a Type=dbus unit started once its bus name appears, which is
            # a little before the VNC listener accepts; give it that moment.
            time.sleep(settle)
            return True
        if state == "failed" and not recovered:
            recovered = True
            run(["systemctl", "--user", "reset-failed", unit])
            run(["systemctl", "--user", "start", unit])
        time.sleep(delay)
    return False


def sync_backend_password(
    credentials: Credentials, *, which: Callable[[str], str | None], run=None, capabilities=None
) -> str | None:
    """Push the stored viewer password into a backend that keeps its own copy.

    GNOME Remote Desktop stores the VNC password in the keyring, so `set-password`
    must also update it there. The secret travels over stdin, never argv.

    Writing the keyring is not enough: the running daemon reads the password when its
    VNC server starts and caches it, so until it is restarted the server keeps
    enforcing the OLD password while everything reports success. That silent
    disagreement makes the new password simply not work, so the daemon is restarted
    here. Any VNC session open at that moment is dropped, which is the expected cost
    of changing the password.

    Returns the backend name that was updated, or None when the active backend reads
    our config directly (WayVNC) or is not present.
    """
    run = _run if run is None else run
    backend, _reason = select_backend(collect() if capabilities is None else capabilities)
    grdctl = which("grdctl")
    if backend != "grd" or grdctl is None:
        return None
    if grd_password(run=run, which=which) == credentials.password:
        # Already in force. Restarting would drop live sessions for no reason.
        return "grd"
    result = run([grdctl, "vnc", "set-password"], credentials.password + "\n")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "grdctl could not store the VNC password")
    systemctl = which("systemctl")
    if systemctl is not None:
        restarted = run([systemctl, "--user", "restart", GRD_UNIT])
        if restarted.returncode != 0:
            raise RuntimeError(
                "The password was stored, but GNOME Remote Desktop could not be "
                f"restarted to apply it: {restarted.stderr.strip()}"
            )
        # Returning as soon as systemd reports the restart leaves a window in which the
        # daemon is up but not yet accepting VNC: a phone told the new password and
        # connecting straight away lands in it and fails, which looks like a wrong
        # password. Wait until it is actually serving.
        wait_for_backend(run=run)
    return "grd"


GRD_RESTART_DROPIN = """\
# Installed by wayland-vnc. gnome-remote-desktop's VNC backend in this distribution
# segfaults whenever a VNC session ends, and systemd's default start limit (5 starts
# in 10s) turns a handful of ordinary reconnects into a refusal to start the daemon
# at all, leaving the desktop with no VNC server for minutes. A wider limit still
# catches a genuine crash loop but rides out disconnect crashes.
[Unit]
StartLimitIntervalSec=10s
StartLimitBurst=20
"""


def grd_dropin_path(env: dict | None = None) -> Path:
    """Where the restart-tolerance override for gnome-remote-desktop lives."""
    environment = os.environ if env is None else env
    config = environment.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "systemd/user" / f"{GRD_UNIT}.d" / "10-wayland-vnc-restart.conf"


def ensure_grd_restart_tolerance(*, run: Runner, which, env: dict | None = None) -> bool:
    """Widen the backend's start limit so a disconnect crash cannot strand the desktop.

    Written into the user's own systemd configuration rather than the packaged unit, so
    it applies whichever way wayland-vnc was installed and is removed with the user's
    own config. Returns whether the override was newly written.
    """
    path = grd_dropin_path(env)
    if path.exists() and path.read_text() == GRD_RESTART_DROPIN:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GRD_RESTART_DROPIN)
    systemctl = which("systemctl")
    if systemctl is not None:
        run([systemctl, "--user", "daemon-reload"])
    return True


def serve_grd(credentials: Credentials, host: Host, directory: Path | None = None) -> None:
    """Configure GNOME Remote Desktop's VNC and make sure it is running.

    Mutter offers none of the wlroots capture/input protocols WayVNC needs, but
    Ubuntu's gnome-remote-desktop carries a VNC backend. Enabling it and setting
    dconf keys are D-Bus calls, so they work from inside the sandboxed unit. If VNC
    was already enabled, the user is relying on it: adopt it and never touch its
    password. Only when it was off is it switched on and seeded with our credential.
    """
    which, run = host.which, host.run
    directory = config_dir() if directory is None else directory
    systemctl, grdctl, gsettings = which("systemctl"), which("grdctl"), which("gsettings")
    if not (systemctl and grdctl and gsettings):
        raise RuntimeError(
            "This desktop is served by GNOME Remote Desktop, but grdctl, gsettings or "
            "systemctl was not found on PATH"
        )
    if not _grd_vnc_enabled(gsettings, run):
        for args in (
            [grdctl, "vnc", "enable"],
            [grdctl, "vnc", "set-auth-method", "password"],
            [grdctl, "vnc", "disable-view-only"],
        ):
            if run(args).returncode != 0:
                raise RuntimeError(
                    f"could not configure GNOME Remote Desktop: {' '.join(args[1:])}"
                )
        seeded = run([grdctl, "vnc", "set-password"], credentials.password + "\n")
        if seeded.returncode != 0:
            raise RuntimeError("could not seed the GNOME Remote Desktop VNC password")
        print(
            "wayland-vnc: enabled GNOME Remote Desktop VNC and set its password.", file=sys.stderr
        )
    else:
        print(
            "wayland-vnc: GNOME Remote Desktop VNC is already enabled; its existing "
            "password stays in effect. Change it with 'wayland-vnc set-password'.",
            file=sys.stderr,
        )
        _adopt_grd_password(credentials, host, directory)
    ensure_grd_restart_tolerance(run=run, which=which)
    # Where the daemon listens follows the stored bind (loopback unless local network
    # access was turned on). A daemon GNOME already started at login is running with
    # whatever the drop-in said before, so a changed address means a restart, not
    # just a start.
    address, _port = bind_address(directory)
    changed = ensure_grd_listen_address(address, run=run, which=which)
    verb = "restart" if changed and backend_is_serving(run) else "start"
    if run([systemctl, "--user", verb, GRD_UNIT]).returncode != 0:
        raise RuntimeError(f"could not {verb} {GRD_UNIT}")
    # An active daemon is not a listening one, and a listener is not necessarily ours.
    # At login a second, short-lived session of the same user (a PAM module opening
    # one, a greeter handing over) runs its own gnome-remote-desktop for a few seconds;
    # it holds port 5900, this session's daemon fails to bind and never retries, and
    # the whole session has no VNC server although its unit reports active. So the
    # listener must belong to THIS session's daemon. Otherwise wait for the impostor
    # to go, restart ours, and fail loudly rather than report a server that is not there.
    _require_grd_listener(run, systemctl, host.listener)
    # Deliberately returns instead of blocking. On a GNOME host this service's job is
    # configuration, not serving: gnome-remote-desktop owns the socket and its own
    # lifetime. An earlier version exec'd `systemctl start --wait` to mirror that
    # lifetime, but from inside a unit that call returns non-zero, so the unit failed
    # and restarted in a loop. The unit sets RemainAfterExit=yes, so it reports
    # active once this has succeeded.


def _adopt_grd_password(credentials: Credentials, host: Host, directory: Path) -> None:
    """Make our credentials file say what GNOME Remote Desktop will actually accept.

    The keyring entry is the authority on a GNOME host: the daemon reads it, the
    settings window shows it, and it can change behind our back (GNOME Settings, a
    keyring that did not persist a write across a re-login). A file that disagrees is
    worse than useless -- `wayland-vnc set-password` and every viewer told to use it
    are then refused -- so the file follows the keyring, never the other way round.
    """
    effective = host.grd_password()
    if not effective or effective == credentials.password:
        return
    try:
        Credentials(credentials.username, effective)
    except ValueError:
        return  # grd allows passwords our own rules would reject; leave the file alone
    set_password(directory, read_secret=lambda _prompt: effective, username=credentials.username)
    print(
        "wayland-vnc: GNOME Remote Desktop's stored VNC password differed from ours; "
        f"the credentials file in {directory} now matches the daemon.",
        file=sys.stderr,
    )


def _grd_main_pid(run: Runner, systemctl: str) -> int:
    result = run([systemctl, "--user", "show", "--property=MainPID", "--value", GRD_UNIT])
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _require_grd_listener(run: Runner, systemctl: str, listener) -> None:
    # A zero owner is "unknown", never a match: `_grd_main_pid` answers 0 when systemd
    # gives no MainPID, and `listener_pid` answers 0 when the socket belongs to a
    # process we cannot see into. Comparing those two zeroes would accept exactly the
    # impostor this check exists to catch.
    owner = _grd_main_pid(run, systemctl)
    if owner and wait_for_listener(owner, listener=listener):
        return
    holder = listener(GRD_VNC_PORT)
    print(
        "wayland-vnc: GNOME Remote Desktop is active but port "
        f"{GRD_VNC_PORT} is {'held by another process' if holder else 'not listening'};"
        " waiting for it and restarting the daemon once.",
        file=sys.stderr,
    )
    if holder is not None and not wait_for_port_free(listener=listener):
        raise RuntimeError(
            f"port {GRD_VNC_PORT} stayed held by process {holder}, not by this session's "
            "GNOME Remote Desktop"
        )
    run([systemctl, "--user", "restart", GRD_UNIT])
    owner = _grd_main_pid(run, systemctl)
    if not owner or not wait_for_listener(owner, listener=listener):
        raise RuntimeError(
            f"GNOME Remote Desktop did not open port {GRD_VNC_PORT} even after a restart"
        )


def require_wayland_session(env: dict) -> None:
    """Refuse anything that is not a native Wayland session, including an unknown one.

    XDG_SESSION_TYPE is not guaranteed inside a systemd user unit, so a Wayland display
    counts as the same evidence; the unit is itself gated on
    `ConditionEnvironment=WAYLAND_DISPLAY`. An unset session type with no Wayland
    display is refused rather than assumed.
    """
    session = env.get("XDG_SESSION_TYPE")
    if session == "wayland" or (session is None and env.get("WAYLAND_DISPLAY")):
        return
    raise RuntimeError("wayland-vnc serves only native Wayland sessions; X11 is refused")


def bind_address(directory: Path) -> tuple[str, int]:
    """The bind already chosen for this user, or the loopback default."""
    existing = read_config(directory)
    return existing if existing is not None else (DEFAULT_ADDRESS, DEFAULT_PORT)


def lan_access(directory: Path) -> bool:
    """Whether local network access is on: the stored bind is a wildcard address."""
    return bind_address(directory)[0] in LAN_ADDRESSES


def provision_preserving_bind(directory: Path, *, generate_key: Callable[[Path], None]) -> Path:
    """Rewrite the config from the stored credential, keeping the configured bind.

    Provisioning with the defaults here would take a server someone had opened to the
    local network, or moved to another port, back to loopback on the next restart,
    silently undoing that choice.
    """
    address, port = bind_address(directory)
    return provision(directory, address=address, port=port, generate_key=generate_key)


GRD_LISTEN_DROPIN = """\
# Installed by wayland-vnc: where GNOME Remote Desktop's VNC listener binds, from the
# "Local Network Access" switch in the Wayland VNC settings (or `wayland-vnc
# provision --address`). Honoured by the private wayland-vnc-grd daemon: unset or
# 127.0.0.1 is this computer only, an empty value means every interface, as
# upstream's daemon does. The distribution's daemon does not read this and always
# listens on every interface.
[Service]
Environment=WAYLAND_VNC_LISTEN_ADDRESS={address}
"""


def grd_listen_dropin_path(env: dict | None = None) -> Path:
    return grd_dropin_path(env).with_name("15-wayland-vnc-listen.conf")


def private_grd_installed(path: Path | None = None) -> bool:
    """Whether the private GNOME Remote Desktop build (wayland-vnc-grd) is on this host.

    Only that build reads WAYLAND_VNC_LISTEN_ADDRESS; with the distribution's daemon
    the VNC listener is on every interface and nothing here can change that.
    """
    daemon = Path(BACKENDS["grd"].executable) if path is None else path
    return daemon.is_file()


def ensure_grd_listen_address(address: str, *, run: Runner, which, env: dict | None = None) -> bool:
    """Tell the private daemon where to listen; returns whether that changed.

    Loopback stays loopback; the wildcard is passed as an empty value so the daemon
    binds every interface exactly as upstream does (IPv4 and IPv6), rather than the
    IPv4-only listener "0.0.0.0" would give it.
    """
    value = "" if address in LAN_ADDRESSES else address
    body = GRD_LISTEN_DROPIN.format(address=value)
    path = grd_listen_dropin_path(env)
    if path.exists() and path.read_text(encoding="utf-8") == body:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    systemctl = which("systemctl")
    if systemctl is not None:
        run([systemctl, "--user", "daemon-reload"])
    return True


def apply_bind(
    directory: Path,
    *,
    which: Callable[[str], str | None],
    run=None,
    capabilities=None,
    private_grd: Callable[[], bool] | None = None,
) -> str | None:
    """Make the stored bind the one the running server actually uses.

    WayVNC reads its config only at start, so our unit is restarted if it is running.
    GNOME Remote Desktop takes the address from its unit environment, so the drop-in
    is rewritten and the daemon restarted when the address changed. Returns the
    backend the bind was applied to, or None when it cannot be: the distribution's
    GNOME Remote Desktop listens on every interface whatever is stored here.
    """
    run = _run if run is None else run
    backend, _reason = select_backend(collect() if capabilities is None else capabilities)
    address, _port = bind_address(directory)
    if backend == "grd":
        installed = private_grd_installed if private_grd is None else private_grd
        if not installed():
            return None
        changed = ensure_grd_listen_address(address, run=run, which=which)
        systemctl = which("systemctl")
        if changed and systemctl is not None and backend_is_serving(run):
            restarted = run([systemctl, "--user", "restart", GRD_UNIT])
            if restarted.returncode != 0:
                raise RuntimeError(
                    "The bind address was stored, but GNOME Remote Desktop could not be "
                    f"restarted to apply it: {restarted.stderr.strip()}"
                )
            wait_for_backend(run=run)
        return "grd"
    if backend != "wayvnc":
        # w0vncserver runs with -localhost by construction; nothing else serves.
        return None
    restart_service(which=which, run=run)
    return "wayvnc"


def serve(
    directory: Path | None = None,
    *,
    env: dict | None = None,
    host: Host,
    generate_key: Callable[[Path], None] = default_key_generator,
    capabilities: dict | None = None,
) -> None:
    """Provision if needed, then serve through the backend this desktop can run.

    The probe decides: GNOME (Mutter) is served through GNOME Remote Desktop, and
    everything else through WayVNC. Whatever the backend, the process never serves
    unauthenticated.
    """
    environment = os.environ if env is None else env
    require_wayland_session(environment)
    directory = config_dir(env) if directory is None else directory
    credentials, generated = ensure_credentials(directory)
    if generated:
        print(
            "wayland-vnc: no viewer password was set, so a random one was generated and "
            f"stored in {directory / CREDENTIALS_NAME}. View or change it in the "
            "Wayland VNC settings app or with 'wayland-vnc set-password'.",
            file=sys.stderr,
        )
    backend, reason = select_backend(collect() if capabilities is None else capabilities)
    if backend == "grd":
        serve_grd(credentials, host, directory)
        return
    # Everything that is not WayVNC is refused by name rather than falling through to
    # it: launching WayVNC on a desktop the probe said needs another backend -- Plasma,
    # served by w0vncserver -- would start a server that cannot capture the session,
    # and launching it when the probe found no backend at all would serve nothing.
    if backend is None:
        raise RuntimeError(f"Refusing to serve: {reason}")
    if backend != "wayvnc":
        raise RuntimeError(
            f"This desktop needs the {backend} backend, which this package does not "
            f"install: {reason}"
        )
    wayvnc = host.which("wayvnc")
    if wayvnc is None:
        raise RuntimeError("The wayvnc server binary was not found on PATH")
    config_path = provision_preserving_bind(directory, generate_key=generate_key)
    mode = config_path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError("Refusing to serve: configuration file is group/world accessible")
    host.exec_fn(wayvnc, [wayvnc, "--config", str(config_path)])
