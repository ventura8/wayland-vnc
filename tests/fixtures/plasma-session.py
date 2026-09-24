"""Launch a disposable KWin virtual-backend Plasma fixture with genuine portals.

The VNC server is TigerVNC w0vncserver, which captures through the KDE
RemoteDesktop/ScreenCast portals and PipeWire. Nothing here touches the host
desktop, a GPU, or X11.
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
FIXTURE_STATE = ("wayland-", "pipewire-", "env", "pointer", "portal-", "passwd")
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
    # TigerVNC VncAuth storage keeps eight significant characters.
    if private_config.get("username") != "fixture" or not 6 <= len(password) <= 8:
        raise SystemExit("Mounted credential file has invalid fixture credentials")
    if not password.isascii():
        raise SystemExit("Mounted credential file has invalid fixture credentials")
password_file = runtime / "passwd"
key_path = runtime / "rsa.pem"
subprocess.run(
    ["/opt/wayland-vnc/tigervnc/bin/vncpasswd", "-f"],
    input=password + "\n",
    stdout=password_file.open("wb"),
    text=True,
    check=True,
    timeout=10,
)
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
            raise SystemExit(f"{command_name(owner)} exited before creating {pattern}")
        time.sleep(0.1)
    raise SystemExit(f"Timed out waiting for {pattern}")


def command_name(child):
    return Path(child.args[0]).name


signal.signal(signal.SIGTERM, terminate)
try:
    pipewire = start(["/usr/bin/pipewire"])
    wait_socket("pipewire-0", 20, pipewire)
    wireplumber = start(["/usr/bin/wireplumber"])
    # FIXTURE_DRM=1: a KVM guest. KWin runs on the virtio-gpu DRM device inside a
    # logind session instead of its virtual backend, with its lock screen (the lock
    # scenario runs there; in a container it never does).
    if os.environ.get("FIXTURE_DRM") == "1":
        # KDE locks the screen on resume by default, so after S3 the viewer saw only
        # the lock screen and suspend-resume could not tell a live session from a dead
        # one. Locking has its own scenario; suspend-resume measures the session alone.
        config = Path.home() / ".config"
        config.mkdir(parents=True, exist_ok=True)
        (config / "kscreenlockerrc").write_text("[Daemon]\nLockOnResume=false\n", encoding="utf-8")
        kwin_command = ["/usr/bin/kwin_wayland", "--no-global-shortcuts"]
    else:
        kwin_command = [
            "/usr/bin/kwin_wayland",
            "--virtual",
            "--width",
            "1920",
            "--height",
            "1080",
            "--no-lockscreen",
            "--no-global-shortcuts",
        ]
    compositor = start(kwin_command)
    os.environ["WAYLAND_DISPLAY"] = wait_socket("wayland-*", 30, compositor).name
    time.sleep(2)
    if os.environ.get("FIXTURE_DRM") == "1":
        # The guest forces its spare output connected (video=<spare>:e) so the
        # monitor-change scenario can enable it; KWin lights every connected output,
        # so the session starts with the spare switched off, as in the container.
        spare_output = os.environ.get("FIXTURE_SPARE_OUTPUT")
        if spare_output:
            subprocess.run(
                ["/usr/bin/kscreen-doctor", f"output.{spare_output}.disable"],
                check=True,
                timeout=20,
            )
    shell_env = {**os.environ, "QT_QUICK_BACKEND": "software"}
    shell = start(["/usr/bin/plasmashell", "--no-respawn"], env=shell_env)
    (runtime / "env").write_text(
        f"WAYLAND_DISPLAY={os.environ['WAYLAND_DISPLAY']}\n"
        f"DBUS_SESSION_BUS_ADDRESS={os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')}\n",
        encoding="utf-8",
    )
    scene_env = {key: value for key, value in os.environ.items() if key != "DISPLAY"}
    scene = start(["/usr/bin/python3", "/fixture/scene.py"], env=scene_env)
    server = start(
        [
            "/opt/wayland-vnc/tigervnc/bin/w0vncserver",
            "-rfbport=5900",
            f"-PasswordFile={password_file}",
            "-SecurityTypes=RA2_256,RA2",
            f"-RSAKey={key_path}",
            "-RequireUsername=0",
            # A persisted approval (the user ticks "allow restoring") must survive
            # daemon restarts, or every restart needs a person at the desktop.
            "-RememberDisplayChoice=Always",
            "-Log=*:stderr:100",
        ]
    )
    start(["/usr/bin/python3", "/fixture/portal-consent.py"])
    # PipeWire, WirePlumber, the shell and the scene are as load-bearing as the
    # compositor here: without them the portal has nothing to offer and the viewer
    # captures an empty desktop. The consent helper is deliberately not watched --
    # it is a long-lived poller and its exit does not invalidate the session.
    required = [pipewire, wireplumber, shell, scene, compositor, server]
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
