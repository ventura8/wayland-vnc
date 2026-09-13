"""Launch a disposable headless GNOME Shell fixture with the private VNC daemon.

GNOME Shell runs Mutter's headless backend with a virtual monitor, no X11 and
software rendering. The private GNOME Remote Desktop build captures through
Mutter's own ScreenCast/RemoteDesktop D-Bus APIs and PipeWire. The VNC password
reaches the daemon only through upstream's test override in its environment.
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
# swept: in a KVM guest (FIXTURE_DRM=1) this directory is logind's /run/user/1000.
# list() first: deleting while the directory iterator is open skips entries.
FIXTURE_STATE = ("wayland-", "pipewire-", "system_bus", "env", "pointer", "portal-")
# FIXTURE_DRM=1: gnome-shell runs on the virtio-gpu DRM device inside a logind session
# instead of Mutter's headless backend, and the machine's own system bus is used.
drm = os.environ.get("FIXTURE_DRM") == "1"
for stale in list(runtime.iterdir()):
    if stale.name == "rsa.pem" or not stale.name.startswith(FIXTURE_STATE):
        continue
    if stale.is_dir() and not stale.is_symlink():
        shutil.rmtree(stale)
    else:
        stale.unlink()
password = secrets.token_urlsafe(6)[:8]
credential_file = os.environ.get("WAYLAND_VNC_CREDENTIAL_FILE")
if credential_file:
    private_config = {}
    for line in Path(credential_file).read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"username", "password"}:
            private_config[key] = value
    password = private_config.get("password", "")
    # Classic VNC authentication keeps eight significant characters.
    if private_config.get("username") != "fixture" or not 6 <= len(password) <= 8:
        raise SystemExit("Mounted credential file has invalid fixture credentials")
    if not password.isascii():
        raise SystemExit("Mounted credential file has invalid fixture credentials")

settings_dir = Path.home() / ".config" / "glib-2.0" / "settings"
settings_dir.mkdir(parents=True, exist_ok=True)
(settings_dir / "keyfile").write_text(
    "[org/gnome/desktop/remote-desktop/vnc]\n"
    "enable=true\nauth-method='password'\nview-only=false\n",
    encoding="utf-8",
)
children = []


def terminate(_signum, _frame):
    raise SystemExit(0)


def start(command, **kwargs):
    child = subprocess.Popen(command, **kwargs)
    children.append(child)
    return child


def wait_socket(pattern, seconds, owner):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        sockets = [p for p in runtime.glob(pattern) if p.is_socket()]
        if sockets:
            return sockets[0]
        if owner.poll() is not None:
            raise SystemExit(f"{Path(owner.args[0]).name} exited before creating {pattern}")
        time.sleep(0.1)
    raise SystemExit(f"Timed out waiting for {pattern}")


signal.signal(signal.SIGTERM, terminate)
try:
    # GNOME Shell requires a system bus; this private one holds no host services.
    system_bus = runtime / "system_bus"
    if drm:
        # The smoke contract names the fixture's system bus socket; here it is the
        # machine's own.
        system_bus.symlink_to("/run/dbus/system_bus_socket")
        bus = None
    else:
        os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = f"unix:path={system_bus}"
        bus = start(
            [
                "/usr/bin/dbus-daemon",
                "--nofork",
                "--config-file=/fixture/gnome/system-bus.conf",
            ]
        )
        wait_socket("system_bus", 10, bus)
    pipewire = start(["/usr/bin/pipewire"])
    wait_socket("pipewire-0", 20, pipewire)
    wireplumber = start(["/usr/bin/wireplumber"])
    shell_command = ["/usr/bin/gnome-shell", "--wayland", "--no-x11"]
    if not drm:
        shell_command += ["--headless", "--virtual-monitor", "1920x1080"]
    compositor = start(shell_command)
    os.environ["WAYLAND_DISPLAY"] = wait_socket("wayland-*", 60, compositor).name
    time.sleep(3)
    if drm:
        # The fixture's mode on the captured connector, as the only monitor: Mutter
        # starts the DRM outputs at their preferred modes, with the second connector
        # enabled, and the scene must not land there.
        subprocess.run(
            [
                "/usr/bin/python3",
                "/fixture/gnome/mutter-monitors.py",
                "mode",
                os.environ.get("FIXTURE_OUTPUT", "Virtual-1"),
                "1920x1080",
                "1",
            ],
            check=True,
            timeout=30,
        )
        time.sleep(2)
    (runtime / "env").write_text(
        f"WAYLAND_DISPLAY={os.environ['WAYLAND_DISPLAY']}\n"
        f"DBUS_SESSION_BUS_ADDRESS={os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')}\n",
        encoding="utf-8",
    )
    scene_env = {key: value for key, value in os.environ.items() if key != "DISPLAY"}
    scene = start(["/usr/bin/python3", "/fixture/scene.py"], env=scene_env)
    server_env = {
        **os.environ,
        "GNOME_REMOTE_DESKTOP_TEST_VNC_PASSWORD": password,
        "GNOME_REMOTE_DESKTOP_DEBUG": "vnc",
        # The private daemon is this computer only unless told otherwise; the viewer
        # harness reaches the fixture through a published port, which Docker forwards
        # to the container's interface rather than its loopback, so the fixture opts
        # in to every interface the way the Local Network Access switch does.
        "WAYLAND_VNC_LISTEN_ADDRESS": "",
    }
    server = start(["/opt/wayland-vnc/grd/libexec/gnome-remote-desktop-daemon"], env=server_env)
    # Every one of these is required for the session to mean anything: without the
    # bus, PipeWire or WirePlumber the daemon captures nothing, and without the scene
    # a viewer would photograph an empty desktop and call it evidence. Watching only
    # the compositor and the server let those die unnoticed.
    required = [c for c in (bus, pipewire, wireplumber, scene, compositor, server) if c]
    while all(child.poll() is None for child in required):
        time.sleep(0.2)
    exited = next(child for child in required if child.poll() is not None)
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
