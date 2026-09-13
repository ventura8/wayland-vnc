"""Supervise the viewer harness: headless Sway with XWayland plus unix-socket WayVNC.

The harness never serves a desktop under test; it only hosts the actual viewer so a
runner can inject input through WayVNC's virtual keyboard and pointer. Its
environment (DISPLAY, WAYLAND_DISPLAY, SWAYSOCK) is written to a file for exec use.
"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

runtime = Path(os.environ["XDG_RUNTIME_DIR"])
runtime.mkdir(mode=0o700, exist_ok=True)
os.umask(0o077)
for stale in list(runtime.iterdir()):
    if stale.is_dir() and not stale.is_symlink():
        shutil.rmtree(stale)
    else:
        stale.unlink()
config = runtime / "wayvnc.conf"
config.write_text("enable_auth=false\n", encoding="utf-8")
input_socket = runtime / "input.sock"
children = []


def terminate(_signum, _frame):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, terminate)
try:
    # DISPLAY is derived from the XWayland socket sway creates, so only a socket that
    # appears after this launch can be it: an X socket already there (a mounted
    # /tmp/.X11-unix, an earlier run in the same container) is somebody else's server.
    x_sockets_before = set(Path("/tmp/.X11-unix").glob("X*"))
    compositor = subprocess.Popen(["/usr/bin/sway", "--config", "/harness/sway.conf"])
    children.append(compositor)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        sockets = [p for p in runtime.glob("wayland-*") if p.is_socket()]
        if sockets:
            os.environ["WAYLAND_DISPLAY"] = sockets[0].name
            break
        if compositor.poll() is not None:
            raise SystemExit("sway failed before creating its Wayland socket")
        time.sleep(0.1)
    else:
        raise SystemExit("Timed out waiting for the harness Wayland socket")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        swaysock = next(iter(runtime.glob("sway-ipc.*.sock")), None)
        if swaysock is not None:
            break
        if compositor.poll() is not None:
            raise SystemExit("sway exited before creating its IPC socket")
        time.sleep(0.1)
    else:
        raise SystemExit("Timed out waiting for the harness sway IPC socket")
    # Sway binds the XWayland socket at startup (lazy server); derive DISPLAY from it.
    # It is not ordered with the IPC socket, so it gets the same bounded wait.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        x_sockets = sorted(set(Path("/tmp/.X11-unix").glob("X*")) - x_sockets_before)
        if x_sockets:
            break
        if compositor.poll() is not None:
            raise SystemExit("sway exited before creating its XWayland socket")
        time.sleep(0.1)
    else:
        # ":0" would be the HOST's X server on a developer machine, so the viewer
        # would open on the real desktop instead of inside this harness.
        raise SystemExit("sway created no XWayland socket; the viewer harness has no display")
    display = f":{x_sockets[0].name[1:]}"
    (runtime / "env").write_text(
        f"WAYLAND_DISPLAY={os.environ['WAYLAND_DISPLAY']}\nSWAYSOCK={swaysock}\n"
        f"DISPLAY={display}\n",
        encoding="utf-8",
    )
    server = subprocess.Popen(
        [
            "/usr/bin/wayvnc",
            "--log-level",
            "info",
            "--config",
            str(config),
            "--unix-socket",
            str(input_socket),
        ]
    )
    children.append(server)
    print(f"harness ready on {display}", flush=True)
    while compositor.poll() is None and server.poll() is None:
        time.sleep(0.2)
    raise SystemExit("Harness process exited")
finally:
    for child in reversed(children):
        child.terminate()
    for child in reversed(children):
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
