"""Launch a disposable headless compositor fixture, never the host's display.

Desktop fixtures go through the distribution's own Wayland session launchers so
that Xfce and LXQt sessions are genuine, not bare labwc with a desktop name.
"""

import os
import secrets
import shutil
import signal
import subprocess
import time
from pathlib import Path

runtime = Path(os.environ["XDG_RUNTIME_DIR"])
runtime.mkdir(mode=0o700, exist_ok=True)
os.umask(0o077)
# A restarted container keeps its previous runtime state; stale sockets and locks
# must not survive, only the server identity does. Only the fixture's own entries are
# swept: in a KVM guest this directory is systemd's real /run/user/1000, whose bus
# and service state belong to the session, not to the fixture.
FIXTURE_STATE = ("wayland-", "sway-ipc.", "wayvnc", "env", "pointer", "portal-", "hypr")
for stale in list(runtime.iterdir()):
    if stale.name == "rsa.pem" or not stale.name.startswith(FIXTURE_STATE):
        continue
    if stale.is_dir() and not stale.is_symlink():
        shutil.rmtree(stale)
    else:
        stale.unlink()
# Disposable credentials are available only through an explicit private file copy.
password = secrets.token_urlsafe(6)
username = "fixture"
credential_file = os.environ.get("WAYLAND_VNC_CREDENTIAL_FILE")
if credential_file:
    private_config = {}
    for line in Path(credential_file).read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"username", "password"}:
            private_config[key] = value
    username = private_config.get("username", username)
    password = private_config.get("password", "")
    if username != "fixture" or not 6 <= len(password) <= 64 or not password.isascii():
        raise SystemExit("Mounted credential file has invalid fixture credentials")
# The session lock authenticates the disposable account against /etc/shadow, which the
# fixture image leaves writable for this account only; no privileged helper is used.
# Where the lock scenario runs, an unwritable file is fatal -- carrying on would leave
# the account's old password in place and the scenario would fail later as a wrong
# password. (Hyprland runs it in its KVM guest; the container image cannot start it.)
LOCKABLE = ("sway", "labwc", "xfce-labwc", "lxqt-labwc", "wayfire", "hyprland")
shadow = Path("/etc/shadow")
lockable = os.environ.get("FIXTURE_COMPOSITOR", "sway") in LOCKABLE
if lockable and not os.access(shadow, os.W_OK):
    raise SystemExit(
        "/etc/shadow is not writable by the fixture account; the lock scenario "
        "cannot authenticate and this fixture must not start pretending otherwise"
    )
if lockable:
    digest = subprocess.run(
        ["/usr/bin/openssl", "passwd", "-6", "-stdin"],
        input=password + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    lines = shadow.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(f"{username}:"):
            fields = line.split(":")
            fields[1] = digest
            lines[index] = ":".join(fields)
            replaced = True
    if not replaced:
        raise SystemExit(f"No /etc/shadow entry for {username!r}; the lock scenario cannot run")
    shadow.write_text("\n".join(lines) + "\n", encoding="utf-8")
config = runtime / "wayvnc.conf"
key_path = runtime / "rsa.pem"
# A private stable key may be mounted so viewers keep one accepted server identity;
# otherwise the identity is generated once and survives a container restart.
server_key_file = os.environ.get("WAYLAND_VNC_SERVER_KEY_FILE")
if server_key_file:
    key_path.write_bytes(Path(server_key_file).read_bytes())
    key_path.chmod(0o600)
if not key_path.exists():
    subprocess.run(
        ["/usr/bin/openssl", "genrsa", "-traditional", "-out", str(key_path), "2048"],
        check=True,
        timeout=20,
    )
config.write_text(
    "address=0.0.0.0\nport=5900\nenable_auth=true\n"
    f"relax_encryption=true\nusername={username}\n"
    f"rsa_private_key_file={key_path}\n"
    f"password={password}\n",
    encoding="utf-8",
)
children = []


def terminate(_signum, _frame):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, terminate)
try:
    compositor_name = os.environ.get("FIXTURE_COMPOSITOR", "sway")
    compositor_commands = {
        "sway": ["/usr/bin/sway", "--config", "/fixture/sway.conf"],
        "labwc": ["/usr/bin/labwc", "--config-dir", "/fixture/labwc"],
        # Xfce's own launcher, told to run labwc with Xfce's labwc configuration and a
        # session command that switches the spare output off before xfce4-session
        # starts (tests/fixtures/xfce-labwc/session.sh explains the order).
        "xfce-labwc": [
            "/usr/bin/startxfce4",
            "--wayland",
            "labwc",
            "--config-dir",
            "/fixture/xfce-labwc",
            "--session",
            "/fixture/xfce-labwc/session.sh",
        ],
        "lxqt-labwc": ["/usr/bin/startlxqtwayland"],
        "wayfire": ["/usr/bin/wayfire", "--config", "/fixture/wayfire.ini"],
        "hyprland": ["/usr/bin/Hyprland", "--config", "/fixture/hyprland.conf"],
    }
    if compositor_name not in compositor_commands:
        raise SystemExit(f"Unsupported fixture compositor: {compositor_name}")
    # These compositors have no sway `create_output`; the monitor-change scenario gets
    # its hot-pluggable output from a second headless output instead, created at
    # start and switched off before anything maps, then switched on and off again
    # through wlr-output-management. To WayVNC that is a wl_output appearing and
    # going away, exactly like a plugged monitor.
    # In a container both outputs are headless (HEADLESS-1 carries the desktop,
    # HEADLESS-2 is the spare); a KVM guest names its DRM output and a single
    # headless spare through the environment instead.
    primary_output = os.environ.get("FIXTURE_OUTPUT", "HEADLESS-1")
    spare_name = os.environ.get("FIXTURE_SPARE_OUTPUT", "HEADLESS-2")
    spare_output = compositor_name in {"labwc", "xfce-labwc", "lxqt-labwc", "wayfire"}
    environment = dict(os.environ)
    if spare_output:
        environment.setdefault("WLR_HEADLESS_OUTPUTS", "2")
    compositor = subprocess.Popen(compositor_commands[compositor_name], env=environment)
    children.append(compositor)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        sockets = [p for p in runtime.glob("wayland-*") if p.is_socket()]
        if sockets:
            os.environ["WAYLAND_DISPLAY"] = sockets[0].name
            break
        if compositor.poll() is not None:
            raise SystemExit(f"{compositor_name} failed before creating its Wayland socket")
        time.sleep(0.1)
    else:
        raise SystemExit("Timed out waiting for isolated Wayland socket")
    randr = []
    if compositor_name not in {"sway", "wayfire", "hyprland"}:
        # Sway, Wayfire and Hyprland set the mode in their configs; labwc sessions get
        # it afterwards.
        randr += ["--output", primary_output, "--custom-mode", "1920x1080"]
    if spare_output:
        randr += ["--output", spare_name, "--off"]
    # Any further output is switched off so the desktop can only be on the captured
    # one: QEMU's virtio-gpu shows a KVM guest a second connector next to the one it
    # was given, and a scene that mapped there would leave the capture empty. In a
    # container the only outputs are the primary and the spare.
    listed = subprocess.run(
        ["/usr/bin/wlr-randr"], capture_output=True, text=True, check=False, timeout=10
    ).stdout
    for line in listed.splitlines():
        name = line.split(" ", 1)[0] if line and not line[0].isspace() else ""
        if name and name not in {primary_output, spare_name}:
            randr += ["--output", name, "--off"]
    if randr:
        subprocess.run(["/usr/bin/wlr-randr", *randr], check=True, timeout=10)
    (runtime / "env").write_text(
        f"WAYLAND_DISPLAY={os.environ['WAYLAND_DISPLAY']}\n"
        f"DBUS_SESSION_BUS_ADDRESS={os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')}\n",
        encoding="utf-8",
    )
    # The output is named: with a spare output present, WayVNC's own default (the
    # first output it hears of) is not a promise the desktop is on it.
    server = subprocess.Popen(
        [
            "/usr/bin/wayvnc",
            "--log-level",
            "trace",
            "--config",
            str(config),
            f"--output={primary_output}",
        ]
    )
    children.append(server)
    while compositor.poll() is None and server.poll() is None:
        time.sleep(0.2)
    exited = compositor if compositor.poll() is not None else server
    raise SystemExit(
        f"Fixture process {Path(exited.args[0]).name} exited with {exited.returncode}; "
        "this is not qualification success"
    )
finally:
    for child in reversed(children):
        child.terminate()
    for child in reversed(children):
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
