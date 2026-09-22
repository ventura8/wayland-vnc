#!/usr/bin/env python3
"""Run the actual RealVNC desktop scenarios against one isolated fixture.

Writes a schema-v2 record into the private evidence root. The record is
``incomplete`` unless every scenario passed; manual input and portal scenarios
are recorded as ``not-run`` by this runner. Credentials never appear on argv.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

from wayland_vnc.android_viewer import Adb, AndroidViewer
from wayland_vnc.desktop_scenarios import (
    HOTPLUG_FIXTURES,
    RESIZABLE_FIXTURES,
    Session,
    fill_record,
    run_scenarios,
)
from wayland_vnc.fixture_smoke import FIXTURES
from wayland_vnc.probe import TARGETS
from wayland_vnc.qemu_guest import QemuGuest
from wayland_vnc.qualification import new_partial_record, validate_record

VIEWERS = {"desktop": "realvnc-desktop", "android": "realvnc-android"}
# The isolated Android lab, as scripts/android-stage.sh sets it up.
ANDROID_AVD_ROOT = Path("artifacts/android")
ANDROID_SERIAL = "emulator-5554"
RENDER_NODE_FIXTURES = ("plasma",)
AUTH_OK = "Authentication successful"
LAB_NETWORK = "wayland-vnc-lab"
HARNESS_IMAGE = "wayland-vnc-viewer-harness:dev"
VERSION_COMMANDS = {
    "hyprland": ["Hyprland", "--version"],
    "sway": ["sway", "--version"],
    "labwc": ["labwc", "--version"],
    "xfce-labwc": ["labwc", "--version"],
    "lxqt-labwc": ["labwc", "--version"],
    "wayfire": ["wayfire", "--version"],
    "gnome": ["gnome-shell", "--version"],
    "plasma": ["kwin_wayland", "--version"],
}
BACKEND_COMMANDS = {
    "gnome": ["sh", "-c", "echo private gnome-remote-desktop 50.2 + LibVNCServer 0.9.15"],
    "plasma": ["/opt/wayland-vnc/tigervnc/bin/w0vncserver", "-version"],
}
# GRD offers only VncAuth, so RealVNC has no server signature to pin and would prompt
# on every connection; the runner disables that prompt for GNOME only and records it.
VIEWER_OPTIONS = {"gnome": ("-VerifyId=0", "-WarnUnencrypted=0")}
# The KVM guest regenerates its WayVNC RSA key on every boot (cloud-init), so the
# viewer cannot pin an identity; the runner disables that check for the guest and
# records it, exactly as it does for GNOME's VncAuth-only server.
VIEWER_OPTIONS["hyprland"] = ("-VerifyId=0", "-WarnUnencrypted=0")
KVM_UNIT = "wayland-vnc-guest.service"
# The guest says which runtime directory its fixture uses (the wlroots guests keep
# one of their own, outside logind's control); the Hyprland guest predates the marker
# and uses logind's.
KVM_FIXTURE_ENV = (
    'XDG_RUNTIME_DIR="$(cat /run/wayland-vnc-guest-runtime 2>/dev/null || echo /run/user/1000)"'
)
# The guest's own copy of the tree (cloud-init copies it out of the 9p share, which
# does not survive suspend); nothing after boot touches /mnt/repo.
KVM_TREE = "/opt/wayland-vnc"
# Written inside the guest once it has been through S3. virtio-gpu does not come
# back whole from suspend: the session that was streaming keeps streaming, but the
# next open of the DRM device blocks in the kernel for ever (seatd and a virtio-gpu
# kworker in uninterruptible sleep), so a guest boot is good for exactly one run.
KVM_SUSPENDED_MARKER = "/run/wayland-vnc-guest-suspended"
RENDERERS = {
    "hyprland": "virtio-gpu KMS through aquamarine, Mesa llvmpipe EGL/GBM (KVM guest)",
    "gnome": "Mutter headless surfaceless software renderer",
    "plasma": "KWin virtual backend, OpenGL through a host DRM render node",
}


DOCKER = shutil.which("docker") or "/usr/bin/docker"
VNCVIEWER = shutil.which("vncviewer") or "/usr/bin/vncviewer"


def _in_session(script):
    """Wrap a fixture-side script so it runs with the session's recorded environment
    (`$XDG_RUNTIME_DIR/env`: WAYLAND_DISPLAY and the session bus); a fixture that
    keeps no such file (the Hyprland guest) runs the script as it is."""
    return (
        '[ -f "$XDG_RUNTIME_DIR/env" ] && export $(cat "$XDG_RUNTIME_DIR/env" | xargs); ' + script
    )


def ensure_harness_image():
    """Build the viewer harness image when this machine has none: a lab machine gets
    the tree by rsync, not the laptop's image store, and the harness holds no viewer
    binary (that is bind-mounted at run time), so building it here is always safe."""
    if sh([DOCKER, "image", "inspect", HARNESS_IMAGE], timeout=30).returncode == 0:
        return
    result = sh(
        [DOCKER, "build", "-q", "-f", "docker/Dockerfile.viewer-harness", "-t", HARNESS_IMAGE, "."],
        timeout=900,
    )
    if result.returncode != 0:
        raise SystemExit(f"could not build {HARNESS_IMAGE}: {result.stderr.strip()}")


def sh(args, timeout=60, **kwargs):
    return subprocess.run(
        args, check=False, capture_output=True, text=True, timeout=timeout, **kwargs
    )


class DockerRealVncDriver:
    """Real side effects: an isolated container and the installed RealVNC Viewer."""

    def __init__(
        self, *, fixture, image, container, port, credential, connection, log_dir, server_key
    ):
        self.fixture = fixture
        self.server_key = server_key
        self.image = image
        self.container = container
        self.port = port
        self.credential = credential
        self.connection = connection
        self.log_dir = log_dir
        self.viewers = 0
        self.live_resize = False

    def start(self, network=None):
        """Run the fixture container: published on the loopback port for a viewer on
        the host, or, with `network`, joined to that internal lab network (unpublished)
        for a viewer that lives in a container of its own."""
        reach = ["--network", network] if network else ["-p", f"127.0.0.1:{self.port}:5900"]
        args = [
            DOCKER,
            "run",
            "-d",
            "--name",
            self.container,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            *reach,
            "-v",
            f"{self.credential}:/run/secrets/fixture.conf:ro",
            "-e",
            "WAYLAND_VNC_CREDENTIAL_FILE=/run/secrets/fixture.conf",
        ]
        if self.server_key is not None:
            args += [
                "-v",
                f"{self.server_key}:/run/secrets/rsa.pem:ro",
                "-e",
                "WAYLAND_VNC_SERVER_KEY_FILE=/run/secrets/rsa.pem",
            ]
        if self.fixture in RENDER_NODE_FIXTURES:
            node = os.environ.get("WAYLAND_VNC_RENDER_NODE", "/dev/dri/renderD128")
            if not Path(node).exists():
                raise SystemExit(f"BLOCKED: {self.fixture} needs a DRM render node ({node} absent)")
            args += ["--device", node, "--group-add", str(os.stat(node).st_gid)]
        result = sh([*args, self.image])
        if result.returncode != 0:
            raise SystemExit(f"docker run failed: {result.stderr.strip()}")

    def stop(self):
        # Keep the fixture's own log (no credentials are ever printed by the fixtures).
        logs = sh([DOCKER, "logs", "--tail", "400", self.container], timeout=60)
        state = sh([DOCKER, "inspect", "--format", "{{json .State}}", self.container], timeout=30)
        (self.log_dir / "container.log").write_text(
            state.stdout + logs.stdout + logs.stderr, encoding="utf-8", errors="replace"
        )
        sh([DOCKER, "rm", "-f", self.container], timeout=60)

    def smoke(self, width, height):
        result = sh(
            [
                DOCKER,
                "exec",
                "-e",
                "PYTHONPATH=/fixture",
                self.container,
                "python3",
                "-m",
                "wayland_vnc.fixture_smoke",
                "--fixture",
                self.fixture,
                "--width",
                str(width),
                "--height",
                str(height),
                "--timeout",
                "90",
            ],
            timeout=120,
        )
        (self.log_dir / f"smoke-{width}x{height}-{int(time.time())}.json").write_text(
            result.stdout, encoding="utf-8"
        )
        return result.returncode == 0

    def connect(self):
        self.viewers += 1
        log_path = self.log_dir / f"viewer-{self.viewers:03d}.log"
        log = log_path.open("wb")
        process = subprocess.Popen(
            [
                VNCVIEWER,
                "-AutoReconnect=0",
                "-EnableUdpRfb=False",
                "-ProxyTcpRfb=0",
                "-PasswordStoreOffer=0",
                "-Log=*:stderr:30",
                *VIEWER_OPTIONS.get(self.fixture, ()),
                "-config",
                str(self.connection),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.close()
        process.log_path = log_path
        return process

    def screenshot(self, session, path):
        # A screenshot request during the RA2 handshake can stall the viewer before it
        # sends credentials, so wait for the viewer's own authentication verdict first.
        log = session.log_path.read_text(errors="replace")
        if AUTH_OK not in log:
            return False
        result = sh([VNCVIEWER, "-screenshot", str(session.pid), str(path)], timeout=15)
        return result.returncode == 0 and path.exists() and path.stat().st_size > 0

    def disconnect(self, session, *, kill=False):
        try:
            os.killpg(session.pid, signal.SIGKILL if kill else signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            session.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(session.pid, signal.SIGKILL)
            session.wait(timeout=10)

    def alive(self, session):
        if session.poll() is not None:
            return False
        return "AuthFailure" not in session.log_path.read_text(errors="replace")

    def transient_error(self, session):
        # The RealVNC RA2 handshake occasionally reports a bad-length RSA on a rapid
        # reconnect; that is a viewer-side transient, not a server refusal. So is a
        # connection that ends before authentication ever happened: on the KVM path
        # QEMU's user-mode NAT drops a fresh connection now and then right after a
        # killed one, and the server never saw more than the TCP open.
        log = session.log_path.read_text(errors="replace")
        if "RSA decrypt/check error" in log or "bad length" in log:
            return True
        return "close: [EndOfStream]" in log and AUTH_OK not in log

    def pause(self, seconds):
        sh([DOCKER, "pause", self.container])
        time.sleep(seconds)
        sh([DOCKER, "unpause", self.container])

    def restart(self):
        result = sh([DOCKER, "restart", "-t", "10", self.container], timeout=90)
        if result.returncode != 0:
            raise OSError(f"docker restart failed: {result.stderr.strip()}")

    # The output that carries the desktop and the one the monitor-change scenario
    # plugs in: both headless in a container; a KVM guest names its DRM output and
    # its single headless spare instead.
    primary_output = "HEADLESS-1"
    spare_output = "HEADLESS-2"

    def _output_lists_mode(self, width, height):
        """Whether the primary output advertises a mode of that size. A headless output
        advertises none and takes any custom mode; a DRM connector (the KVM guest's
        virtio-gpu) lists what it can scan out, and commits a custom timing only
        sometimes -- 1280x720 yes, a CVT 3840x2160 no -- so a listed mode is used
        whenever there is one."""
        listed = self._fixture_exec("wlr-randr", timeout=15).stdout
        block = ""
        for line in listed.splitlines():
            if line and not line[0].isspace():
                block = line.split(" ", 1)[0]  # a new output's block starts
            elif block == self.primary_output and f" {width}x{height} px" in line:
                return True
        return False

    # Capabilities the fixture's machine adds beyond what its name says (a KVM guest
    # gives the desktops real DRM outputs and their own lock screens); none here.
    machine_capabilities = ()

    def set_mode(self, width, height, scale):
        if self.fixture not in RESIZABLE_FIXTURES and "resize" not in self.machine_capabilities:
            raise ValueError("fixture output mode is not runner-controlled")
        if self.fixture == "gnome":
            script = (
                f"python3 /fixture/gnome/mutter-monitors.py mode {self.primary_output}"
                f" {width}x{height} {scale}"
            )
        elif self.fixture == "plasma":
            script = (
                f"kscreen-doctor output.{self.primary_output}.mode.{width}x{height}@60"
                f" output.{self.primary_output}.scale.{scale}"
            )
        elif self.fixture == "hyprland":
            script = (
                f"hyprctl --instance 0 keyword monitor"
                f" {self.primary_output},{width}x{height}@60,0x0,{scale}"
            )
        elif self.fixture == "sway":
            # Sway 1.11 segfaults on headless mode changes while WayVNC holds capture
            # state (wlr-randr and IPC alike). Detach WayVNC around the change; the
            # live resize scenario still changes the mode with the client attached.
            attached = self.live_resize
            custom = "" if self._output_lists_mode(width, height) else "--custom "
            script = (
                'export SWAYSOCK="$(ls "$XDG_RUNTIME_DIR"/sway-ipc.*.sock | head -1)"'
                ' WAYLAND_DISPLAY="$(ls "$XDG_RUNTIME_DIR" | grep -E "^wayland-[0-9]+$" | head -1)"'
                + ("" if attached else " && wayvncctl detach && sleep 1")
                # Quoted as one command, or swaymsg reads --custom as an option of its own.
                + f" && swaymsg 'output {self.primary_output} mode {custom}{width}x{height}"
                + f" scale {scale}'"
                + ("" if attached else ' && sleep 1 && wayvncctl attach "$WAYLAND_DISPLAY"')
            )
        else:
            option = "--mode" if self._output_lists_mode(width, height) else "--custom-mode"
            script = (
                f"wlr-randr --output {self.primary_output} {option} {width}x{height}"
                f" --scale {scale}"
            )
        result = self._fixture_exec(script, timeout=30)
        if result.returncode != 0:
            raise OSError(f"mode change failed: {result.stderr.strip()}")
        time.sleep(3)

    def wait_idle(self):
        """Wait until WayVNC reports no clients; other backends have no idle query."""
        if self.fixture not in RESIZABLE_FIXTURES or self.fixture in ("gnome", "plasma"):
            return True
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self._fixture_exec("wayvncctl --json client-list", timeout=15)
            if result.returncode == 0 and result.stdout.strip() == "[]":
                # Let WayVNC finish tearing down its capture before the mode changes.
                time.sleep(3)
                return True
            time.sleep(0.5)
        return False

    def portal_mode(self, mode):
        """Select what the in-fixture consent helper does with the next KDE dialog."""
        if mode not in ("approve", "approve-persist", "deny", "none"):
            raise ValueError(f"unknown portal consent mode {mode!r}")
        script = f'printf "%s\\n" "{mode}" > "$XDG_RUNTIME_DIR/portal-consent-mode"'
        result = sh([DOCKER, "exec", self.container, "sh", "-c", script], timeout=30)
        if result.returncode != 0:
            raise OSError(f"could not set the consent mode: {result.stderr.strip()}")

    def portal_forget(self):
        """Delete w0vncserver's stored restore token with the server's own tool."""
        result = sh(
            [DOCKER, "exec", self.container, "/opt/wayland-vnc/tigervnc/bin/w0vncserver-forget"],
            timeout=30,
        )
        if result.returncode != 0 and "No such file" not in result.stderr:
            raise OSError(f"w0vncserver-forget failed: {(result.stderr or result.stdout).strip()}")

    def portal_events(self):
        script = 'cat "$XDG_RUNTIME_DIR/portal-consent.jsonl" 2>/dev/null || true'
        result = sh([DOCKER, "exec", self.container, "sh", "-c", script], timeout=30)
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def _fixture_exec(self, script, timeout=60):
        """Run a shell script inside the fixture with the session's environment (the
        Wayland display and session bus the session script recorded)."""
        command = [DOCKER, "exec", self.container, "sh", "-c", _in_session(script)]
        return sh(command, timeout=timeout)

    def lock(self):
        """Engage the fixture's real session lock: the desktop's own screen saver on
        GNOME and Plasma (their lock screens), ext-session-lock through swaylock on the
        wlroots compositors. swaylock is detached from the caller's pipes: daemonised,
        it would otherwise keep the guest agent waiting for their end of file."""
        if self.fixture == "gnome":
            command = (
                "gdbus call --session --dest org.gnome.ScreenSaver"
                " --object-path /org/gnome/ScreenSaver --method org.gnome.ScreenSaver.Lock"
            )
        elif self.fixture == "plasma":
            command = (
                "dbus-send --session --print-reply --dest=org.freedesktop.ScreenSaver"
                " /ScreenSaver org.freedesktop.ScreenSaver.Lock"
            )
        else:
            command = (
                "swaylock --daemonize --color 101010 --ignore-empty-password"
                " >/dev/null 2>&1 </dev/null"
            )
        result = self._fixture_exec(command)
        if result.returncode != 0:
            raise OSError(f"lock failed: {result.stderr.strip()}")

    def force_unlock(self):
        if self.fixture == "gnome":
            self._fixture_exec(
                "gdbus call --session --dest org.gnome.ScreenSaver"
                " --object-path /org/gnome/ScreenSaver"
                " --method org.gnome.ScreenSaver.SetActive false || true"
            )
        elif self.fixture == "plasma":
            self._fixture_exec("loginctl unlock-sessions || true")
        else:
            self._fixture_exec("pkill -x swaylock || true")

    def hotplug(self, attach):
        if self.fixture not in HOTPLUG_FIXTURES and "hotplug" not in self.machine_capabilities:
            raise ValueError("this fixture cannot hot-plug outputs")
        if self.fixture == "gnome":
            state = "on" if attach else "off"
            helper = "python3 /fixture/gnome/mutter-monitors.py"
            result = self._fixture_exec(f"{helper} enable {self.spare_output} {state}")
        elif self.fixture == "plasma":
            state = "enable" if attach else "disable"
            result = self._fixture_exec(f"kscreen-doctor output.{self.spare_output}.{state}")
        elif self.fixture == "sway":
            swaysock = 'export SWAYSOCK="$(ls "$XDG_RUNTIME_DIR"/sway-ipc.*.sock | head -1)" && '
            command = (
                "swaymsg create_output" if attach else f"swaymsg output {self.spare_output} unplug"
            )
            result = self._fixture_exec(swaysock + command)
        elif self.fixture == "hyprland":
            command = (
                "hyprctl --instance 0 output create headless"
                if attach
                else f"hyprctl --instance 0 output remove {self.spare_output}"
            )
            result = self._fixture_exec(command)
        else:
            # The spare headless output the session script created and switched off.
            state = "--on" if attach else "--off"
            result = self._fixture_exec(f"wlr-randr --output {self.spare_output} {state}")
        if result.returncode != 0:
            raise OSError(f"output hot-plug failed: {result.stderr.strip()}")

    def versions(self):
        def inside(command):
            result = sh([DOCKER, "exec", self.container, *command], timeout=30)
            return " ".join((result.stdout + result.stderr).split()) or "unknown"

        compositor = inside(VERSION_COMMANDS[self.fixture])
        backend = inside(BACKEND_COMMANDS.get(self.fixture, ["wayvnc", "--version"]))
        distribution = inside(["sh", "-c", '. /etc/os-release && echo "$PRETTY_NAME"'])
        return self._versions(compositor, backend, distribution + " (container)")

    def _versions(self, compositor, backend, distribution):
        viewer = "unknown"
        for log in sorted(self.log_dir.glob("viewer-*.log")):
            match = re.search(r"RealVNC\(R\) Viewer [^\n]+", log.read_text(errors="replace"))
            if match:
                viewer = match.group(0).strip()
                break
        renderer = RENDERERS.get(self.fixture, "wlroots headless backend, pixman renderer")
        options = " ".join(VIEWER_OPTIONS.get(self.fixture, ()))
        return {
            "compositor": compositor,
            "backend": backend,
            "viewer": viewer + (f" (runner options: {options})" if options else ""),
            "distribution": distribution,
            "renderer": renderer,
        }


class HarnessViewerMixin:
    """Viewer side shared by the drivers that run the actual desktop viewer inside the
    isolated harness container: input is injected through the harness compositor's
    WayVNC virtual keyboard and pointer, so keyboard, pointer, scroll and drag are
    unattended. The fixture side (a container or a KVM guest) comes from the class
    this is mixed into."""

    supports_input = True

    def _harness_init(self, identities, viewer_config):
        self.identities = identities
        self.viewer_config = viewer_config
        self.harness = f"{self.container}-harness"
        self.pids = []

    def start_harness(self, host, port, *, network):
        """Start the harness container on `network` and point its viewer at host:port
        (a fixture container's name on the lab network, or the lab bridge address a
        KVM guest's VNC port is forwarded to)."""
        viewer = shutil.which("vncviewer")
        if viewer is None:
            raise SystemExit("the installed RealVNC Viewer binary was not found")
        ensure_harness_image()
        result = sh(
            [
                DOCKER,
                "run",
                "-d",
                "--name",
                self.harness,
                "--network",
                network,
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "-v",
                f"{Path(viewer).resolve()}:/usr/bin/vncviewer:ro",
                "-v",
                f"{self.log_dir.resolve()}:/out",
                HARNESS_IMAGE,
            ],
            timeout=60,
        )
        if result.returncode != 0:
            raise SystemExit(f"harness start failed: {result.stderr.strip()}")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            probe = sh(
                [DOCKER, "exec", self.harness, "sh", "-c", 'test -S "$XDG_RUNTIME_DIR/input.sock"'],
                timeout=15,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise SystemExit("harness compositor did not come up")
        # The viewer's pinned identity and EULA state, rewritten for the lab address.
        identities = self.identities.read_text(encoding="utf-8").splitlines()
        # Each line is `host::port=...` (or `host::port/extra=...`); the whole
        # recorded prefix is replaced, so a five-digit port leaves no stray digit,
        # and the port is the LAST `::` so a bracketed IPv6 host is not cut in two.
        prefix = re.compile(r"^.*::\d+(?=[=/]|$)")
        rewritten = [
            prefix.sub(f"{host}::{port}", line, count=1)
            for line in identities
            if prefix.match(line)
        ]
        staged = self.log_dir / ".harness-identities"
        staged.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
        sh([DOCKER, "cp", str(staged), f"{self.harness}:/home/fixture/.vnc/identities"])
        staged.unlink()
        sh([DOCKER, "exec", self.harness, "mkdir", "-p", "/home/fixture/.vnc/config.d"])
        sh(
            [
                DOCKER,
                "cp",
                str(self.viewer_config),
                f"{self.harness}:/home/fixture/.vnc/config.d/vncviewer",
            ]
        )
        connection = self.connection.read_text(encoding="utf-8")
        connection = re.sub(r"^Host=.*$", f"Host={host}:{port}", connection, flags=re.M)
        staged = self.log_dir / ".harness-connection.vnc"
        staged.write_text(connection, encoding="utf-8")
        sh([DOCKER, "cp", str(staged), f"{self.harness}:/home/fixture/connection.vnc"])
        staged.unlink()
        sh([DOCKER, "exec", self.harness, "chmod", "600", "/home/fixture/connection.vnc"])

    def stop_harness(self):
        sh([DOCKER, "rm", "-f", self.harness], timeout=60)

    def _exec(self, script, timeout=60):
        wrapped = 'export $(cat "$XDG_RUNTIME_DIR/env" | xargs) && ' + script
        return sh([DOCKER, "exec", self.harness, "sh", "-c", wrapped], timeout=timeout)

    def connect(self):
        self.viewers += 1
        log_name = f"viewer-{self.viewers:03d}.log"
        options = " ".join(VIEWER_OPTIONS.get(self.fixture, ()))
        script = (
            "vncviewer -AutoReconnect=0 -EnableUdpRfb=False -ProxyTcpRfb=0 -PasswordStoreOffer=0"
            f" -Log='*:stderr:30' {options} -config /home/fixture/connection.vnc"
            f" > /out/{log_name} 2>&1 & echo $!"
        )
        result = self._exec(script)
        lines = result.stdout.strip().splitlines()
        if result.returncode != 0 or not lines or not lines[-1].strip().isdigit():
            detail = (result.stderr or result.stdout).strip()
            raise OSError(f"could not start the viewer in the harness: {detail}")
        pid = int(lines[-1].strip())
        session = HarnessSession(pid, self.log_dir / log_name, self)
        self.pids.append(pid)
        return session

    def screenshot(self, session, path):
        if AUTH_OK not in session.log_path.read_text(errors="replace"):
            return False
        result = self._exec(f"vncviewer -screenshot {session.pid} /out/{path.name}", timeout=20)
        return result.returncode == 0 and path.exists() and path.stat().st_size > 0

    def disconnect(self, session, *, kill=False):
        signal_name = "KILL" if kill else "TERM"
        self._exec(f"kill -{signal_name} {session.pid} 2>/dev/null || true")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.alive(session):
            time.sleep(0.25)
        if self.alive(session):
            self._exec(f"kill -KILL {session.pid} 2>/dev/null || true")

    def alive(self, session):
        if self._exec(f"kill -0 {session.pid} 2>/dev/null").returncode != 0:
            return False
        return "AuthFailure" not in session.log_path.read_text(errors="replace")

    def _inject(self, *arguments):
        args = " ".join(str(a) for a in arguments)
        result = self._exec(
            f'python3 /harness/rfb-input.py --socket "$XDG_RUNTIME_DIR/input.sock" {args}'
        )
        if result.returncode != 0:
            raise OSError(f"input injection failed: {result.stderr.strip()}")

    def type_text(self, text):
        if text == "\n":
            self._inject("key", "0xff0d")
            return
        if not text.replace("-", "").isalnum():
            raise ValueError("only alphanumeric text is typed")
        self._inject("type", text)

    def type_secret(self):
        """Type the fixture password then Return into the lock, reading it from the
        private credential file over stdin. A warm-up key and Backspace absorb the
        first-keystroke drop that a freshly focused virtual keyboard exhibits, so the
        secret buffer ends up exactly the password."""
        password = ""
        for line in self.credential.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key == "password":
                password = value
        if not password:
            # Typing nothing and Return would be a wrong-password unlock attempt that
            # reads as a scenario failure; the missing line is the real fault.
            raise OSError(f"{self.credential} has no password= line; refusing to type a secret")
        wrapped = (
            'export $(cat "$XDG_RUNTIME_DIR/env" | xargs) && '
            'python3 /harness/rfb-input.py --socket "$XDG_RUNTIME_DIR/input.sock" secret'
        )
        result = subprocess.run(
            [DOCKER, "exec", "-i", self.harness, "sh", "-c", wrapped],
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise OSError(f"typing the unlock secret failed: {detail}")

    def click(self, x_pos, y_pos):
        self._inject("click", x_pos, y_pos)

    def scroll(self, x_pos, y_pos):
        self._inject("scroll", x_pos, y_pos)

    def drag(self, x_from, y_from, x_to, y_to):
        self._inject("drag", x_from, y_from, x_to, y_to)


class HarnessDriver(HarnessViewerMixin, DockerRealVncDriver):
    """Container fixture plus the harness viewer, both on the internal lab network."""

    def __init__(self, *, identities, viewer_config, **kwargs):
        super().__init__(**kwargs)
        self._harness_init(identities, viewer_config)

    def start(self, network=LAB_NETWORK):
        """The viewer lives in a container of its own, so both ends join the internal
        lab network; a caller that names another one is honoured."""
        _ensure_lab_network()
        DockerRealVncDriver.start(self, network=network)
        self.start_harness(self.container, 5900, network=network)

    def stop(self):
        DockerRealVncDriver.stop(self)
        self.stop_harness()


def _ensure_lab_network():
    """Create the lab bridge if missing. Not --internal: a KVM guest's VNC port is
    forwarded to this bridge's gateway on the host, and a container on an --internal
    network cannot reach a service the host binds on the gateway. The guest port is
    still off the LAN -- it is on the docker bridge gateway, not a routable address --
    which is the isolation that matters."""
    sh([DOCKER, "network", "create", LAB_NETWORK], timeout=30)


class KvmDriver(DockerRealVncDriver):
    """The fixture is a KVM guest, not a container: the actual viewer runs on the host
    against the guest's forwarded loopback port, fixture-side commands go through the
    QEMU guest agent, and the machine can really suspend -- the one scenario no
    container can provide."""

    supports_suspend = True
    # The guest's DRM output carries the desktop; its headless sub-backend provides
    # the spare (or, for sway, the output created on request), always named first.
    primary_output = "Virtual-1"
    spare_output = "HEADLESS-1"

    def __init__(self, *, work, **kwargs):
        super().__init__(**kwargs)
        self.work = work
        self.guest = QemuGuest(work)
        if self.fixture == "hyprland":
            # Hyprland numbers the headless output it creates on request from 2.
            self.spare_output = "HEADLESS-2"
        if self.fixture in ("gnome", "plasma"):
            # Real DRM outputs and the desktop's own lock screen: the guest gives the
            # desktops what their headless containers cannot. The spare is virtio-gpu's
            # second connector.
            self.machine_capabilities = ("resize", "hotplug", "lock")
            self.spare_output = "Virtual-2"

    def start(self, network=None):
        # The fixture is a virtual machine, not a container: there is no Docker network
        # to join, and the parameter exists only to keep the base class's contract.
        del network
        pid_file = self.work / "qemu.pid"
        if not pid_file.is_file():
            raise SystemExit(f"no running guest under {self.work} (scripts/kvm/build-*.sh first)")
        if not self.guest.wait_ready(120):
            raise SystemExit("the guest agent did not answer; is the guest still booting?")
        ready = self.guest.shell("test -f /run/wayland-vnc-guest-ready", timeout=30)
        if ready.exitcode != 0:
            raise SystemExit("cloud-init has not finished provisioning the guest")
        if self.guest.shell(f"test -f {KVM_SUSPENDED_MARKER}", timeout=30).exitcode == 0:
            raise SystemExit(
                "this guest has already been through suspend-resume; virtio-gpu cannot "
                "reopen its DRM device afterwards, so rebuild the guest for a new run"
            )

    def stop(self):
        # The guest outlives the run (it is disposable, but rebooting it costs minutes);
        # keep its fixture journal the way the container driver keeps `docker logs`.
        # A guest that never answered (start() gave up on it) has no journal to give;
        # that is recorded as such rather than raised over the error that stopped the run.
        try:
            status = json.dumps(self.guest.status())
            journal = self.guest.shell(
                f"journalctl -u {KVM_UNIT} --no-pager | tail -400", timeout=60
            ).stdout
        except OSError as error:
            status, journal = json.dumps({"error": str(error)}), ""
        (self.log_dir / "container.log").write_text(status + "\n" + journal, encoding="utf-8")

    def _fixture(self, script, timeout=60):
        return self.guest.shell(f"export {KVM_FIXTURE_ENV} && {script}", timeout=timeout)

    def _fixture_exec(self, script, timeout=60):
        """The base driver's fixture hooks (mode, hot-plug, lock, idle) run through
        the guest agent here, with the same session environment as in a container --
        and as the fixture account: the agent runs as root, and swaylock started by
        root would authenticate root's password, not the fixture's."""
        as_fixture = (
            'runuser -u "$(stat -c %U "$XDG_RUNTIME_DIR")" -- env'
            ' XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" HOME="$(getent passwd'
            ' "$(stat -c %U "$XDG_RUNTIME_DIR")" | cut -d: -f6)" sh -c '
            + shlex.quote(_in_session(script))
        )
        result = self._fixture(as_fixture, timeout=timeout)
        return subprocess.CompletedProcess([], result.exitcode, result.stdout, result.stderr)

    def smoke(self, width, height):
        stamp = self.log_dir / f"smoke-{width}x{height}-{int(time.time())}.json"
        try:
            # As root, in the session's environment: root sees every process in
            # /proc (in a logind session the fixture account cannot read even its own
            # processes' environment there), and the checker hands the one call that
            # needs the fixture's session bus, Mutter's, to the bus owner itself.
            smoke = (
                f"cd {KVM_TREE} && PYTHONPATH={KVM_TREE}/src python3 -m wayland_vnc.fixture_smoke"
                f" --fixture {self.fixture} --width {width} --height {height} --timeout 90"
            )
            result = self._fixture(_in_session(smoke), timeout=150)
        except OSError as error:
            # A guest that stops answering is a failed health check, recorded as such,
            # not a runner crash that loses the record.
            stamp.write_text(json.dumps({"passed": False, "error": str(error)}), encoding="utf-8")
            return False
        stamp.write_text(result.stdout, encoding="utf-8")
        return result.exitcode == 0

    def pause(self, seconds):
        self.guest.pause(seconds)

    def restart(self):
        """Stop the fixture unit, wait until the compositor is really gone, start it,
        and wait for the Wayland socket and the VNC listener to be back.

        A plain `systemctl restart` races Hyprland's crash-on-exit: the new instance
        starts while the old one still holds the DRM device and blocks for ever.
        """
        compositor = FIXTURES[self.fixture].compositor
        self._fixture(f"systemctl stop {KVM_UNIT}", timeout=60)
        gone = self._await_fixture(f"! pgrep -x {compositor} >/dev/null", 30)
        if not gone:
            # A logind session (the desktop guests' unit opens one) keeps the
            # fixture's processes in a session scope the unit's stop does not reach:
            # everything the fixture account runs goes.
            account = 'pkill -KILL -u "$(stat -c %U "$XDG_RUNTIME_DIR")"'
            leftovers = f"pkill -KILL -x {compositor}; pkill -KILL -x wayvnc"
            self._fixture(f"{account}; {leftovers}", timeout=30)
            if not self._await_fixture(f"! pgrep -x {compositor} >/dev/null", 15):
                raise OSError("the guest compositor would not stop")
        result = self._fixture(f"systemctl start {KVM_UNIT}", timeout=60)
        if result.exitcode != 0:
            raise OSError(f"guest unit start failed: {result.stderr.strip()}")
        up = self._await_fixture(
            'ls "$XDG_RUNTIME_DIR" | grep -q "^wayland-[0-9]" && ss -ltn | grep -q ":5900 "', 90
        )
        if not up:
            raise OSError("the guest fixture did not come back after the restart")

    def _await_fixture(self, condition, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._fixture(condition, timeout=20).exitcode == 0:
                return True
            time.sleep(1)
        return False

    def suspend(self, seconds):
        transcript = self.guest.suspend_and_wake(seconds)
        if transcript.get("agent_back"):
            self.guest.shell(f"touch {KVM_SUSPENDED_MARKER}", timeout=30)
        return transcript

    def versions(self):
        def inside(command):
            result = self._fixture(" ".join(command), timeout=30)
            return " ".join((result.stdout + result.stderr).split()) or "unknown"

        compositor = inside(VERSION_COMMANDS[self.fixture])
        backend = inside(["wayvnc", "--version"])
        distribution = inside(['. /etc/os-release && echo "$PRETTY_NAME"'])
        return self._versions(compositor, backend, distribution + " (KVM guest)")


class KvmHarnessDriver(HarnessViewerMixin, KvmDriver):
    """A KVM guest fixture (real suspend) with the harness viewer (real input): the
    guest's VNC port is forwarded to host loopback (`scripts/kvm/build-*.sh` without a
    bind override), and the harness runs on the host network namespace so it reaches
    it at 127.0.0.1. Loopback keeps the guest off every network but the host itself --
    stricter than a bridge, and free of the host-cannot-be-reached-from-its-own-bridge
    quirk that WSL2's docker networking exhibits."""

    def __init__(self, *, identities, viewer_config, **kwargs):
        super().__init__(**kwargs)
        self._harness_init(identities, viewer_config)

    def start(self):
        KvmDriver.start(self)
        listening = sh(["ss", "-ltnH", f"( sport = :{self.port} )"], timeout=15)
        if f"127.0.0.1:{self.port}" not in listening.stdout:
            raise SystemExit(
                f"the guest's VNC port is not forwarded to 127.0.0.1:{self.port}; build the "
                f"guest with scripts/kvm/build-*.sh --harness"
            )
        self.start_harness("127.0.0.1", self.port, network="host")

    def stop(self):
        KvmDriver.stop(self)
        self.stop_harness()


class HarnessSession:
    """A viewer process inside the harness container."""

    def __init__(self, pid, log_path, driver):
        self.pid = pid
        self.log_path = log_path
        self.driver = driver


class AndroidSession:
    """One connection of the Android app; the app itself is the session."""

    def __init__(self, index, log_path, connected, started_at):
        self.index = index
        self.log_path = log_path
        self.connected = connected
        # The scenarios time the first frame from here (see connect_and_capture).
        self.started_at = started_at


class AndroidViewerMixin:
    """Viewer side shared by the drivers that run the actual RealVNC Viewer for
    Android in the isolated emulator: the app reaches the fixture's port through
    `adb reverse`, and input goes through the app's own gestures
    (wayland_vnc.android_viewer). The fixture side (a container or a KVM guest) comes
    from the class this is mixed into, so every fixture-side scenario runs unchanged."""

    supports_input = True
    # The app's unlock types a warm-up key first, which wakes a desktop lock screen;
    # the scenario must not also type into the field.
    wakes_own_lock_screen = True

    def _android_init(self, viewer):
        self.viewer = viewer
        self.viewer.pointer_report = self._pointer

    def start(self):
        super().start()
        self.viewer.prepare(self.port)

    def stop(self):
        self.viewer.disconnect()
        super().stop()

    def _pointer(self):
        result = self._fixture_exec('cat "$XDG_RUNTIME_DIR/pointer" 2>/dev/null', timeout=20)
        parts = result.stdout.split()
        if len(parts) != 2 or not all(part.lstrip("-").isdigit() for part in parts):
            return None
        return int(parts[0]), int(parts[1])

    def _credentials(self):
        fields = {}
        for line in self.credential.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in ("username", "password"):
                fields[key] = value
        if not fields.get("password"):
            raise OSError(f"{self.credential} has no password= line")
        return fields.get("username", "fixture"), fields["password"]

    def connect(self):
        self.viewers += 1
        log_path = self.log_dir / f"viewer-{self.viewers:03d}.log"
        username, password = self._credentials()
        started = time.monotonic()
        outcome = self.viewer.connect(username, password)
        # The app has no log of its own to keep; what it showed on the way is.
        submitted = outcome.submitted_at or started
        log_path.write_text(
            f"{self.viewer.version()}\nsteps: {' > '.join(outcome.steps)}\n"
            f"app dialogs answered in {submitted - started:.1f}s before the server was asked\n"
            + "".join(f"{note}\n" for note in outcome.notes)
            + (f"error: {outcome.error}\n" if outcome.error else AUTH_OK + "\n"),
            encoding="utf-8",
        )
        return AndroidSession(self.viewers, log_path, outcome.connected, submitted)

    def screenshot(self, session, path):
        if not session.connected:
            return False
        return self.viewer.screenshot(path) and path.stat().st_size > 0

    def disconnect(self, session, *, kill=False):
        # Stopping the app is the only disconnect it has; kill and graceful coincide.
        del kill
        self.viewer.disconnect()
        session.connected = False

    def alive(self, session):
        return session.connected and self.viewer.desktop_visible()

    def transient_error(self, session):
        del session
        return self.viewer.transient_error()

    def type_text(self, text):
        self.viewer.type_text(text)

    def type_secret(self):
        _username, password = self._credentials()
        self.viewer.type_unlock_secret(password)

    def click(self, x_pos, y_pos):
        self.viewer.click(x_pos, y_pos)

    def scroll(self, x_pos, y_pos):
        self.viewer.scroll(x_pos, y_pos)

    def drag(self, x_from, y_from, x_to, y_to):
        self.viewer.drag(x_from, y_from, x_to, y_to)

    def framebuffer_size(self, session):
        """What the app reports as the desktop size; the mode scenarios compare it,
        since a phone screen capture is always the phone's own size."""
        if not session.connected:
            return None
        size = self.viewer.desktop_size()
        if size is None:
            reason = self.viewer.size_report_failure
            raise OSError(f"the app did not report its desktop size: {reason}")
        return size

    def versions(self):
        versions = super().versions()
        versions["viewer"] = self.viewer.version()
        return versions


class AndroidDriver(AndroidViewerMixin, DockerRealVncDriver):
    """The Android app against a container fixture."""

    def __init__(self, *, viewer, **kwargs):
        super().__init__(**kwargs)
        self._android_init(viewer)


class KvmAndroidDriver(AndroidViewerMixin, KvmDriver):
    """The Android app against a KVM guest (real suspend): the guest's VNC port is
    forwarded to host loopback (`scripts/kvm/build-guest.sh` without `--harness`),
    which is where `adb reverse` points the app."""

    def __init__(self, *, viewer, **kwargs):
        super().__init__(**kwargs)
        self._android_init(viewer)


def android_viewer(sdk):
    """The isolated AVD's app, through the project's own adb keys (never a personal
    Android configuration)."""
    root = ANDROID_AVD_ROOT.resolve()
    env = {
        **os.environ,
        "ANDROID_SDK_ROOT": str(sdk),
        "ANDROID_AVD_HOME": str(root / "avd"),
        "ANDROID_USER_HOME": str(root / "user"),
    }
    adb = Adb(str(sdk / "platform-tools" / "adb"), ANDROID_SERIAL, env)
    return AndroidViewer(adb, pointer_report=lambda: None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", choices=TARGETS, required=True)
    parser.add_argument("--port", type=int, required=True, help="loopback port for the fixture")
    parser.add_argument(
        "--viewer",
        choices=sorted(VIEWERS),
        default="desktop",
        help="the actual RealVNC Viewer to drive: desktop (default) or android",
    )
    parser.add_argument(
        "--sdk",
        type=Path,
        default=Path(os.environ.get("ANDROID_SDK_ROOT", Path.home() / "Android" / "Sdk")),
        help="Android SDK root (--viewer android)",
    )
    parser.add_argument(
        "--connection", type=Path, default=None, help="private RealVNC .vnc file (desktop viewer)"
    )
    parser.add_argument("--credential", type=Path, required=True, help="private fixture.conf")
    parser.add_argument(
        "--server-key",
        type=Path,
        default=None,
        help="private PEM key giving the fixture a stable, already-accepted RA2 identity",
    )
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--image", default=None)
    parser.add_argument(
        "--harness",
        action="store_true",
        help="run the viewer inside the isolated harness container and inject input",
    )
    parser.add_argument(
        "--kvm",
        type=Path,
        default=None,
        help="drive a running QEMU guest (its socket directory) instead of a container",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=None,
        help="run only these scenarios (repeatable); for targeted re-runs, not full qualification",
    )
    parser.add_argument("--identities", type=Path, default=None, help="private pinned identities")
    parser.add_argument("--viewer-config", type=Path, default=None, help="private viewer config")
    args = parser.parse_args()
    viewer_name = VIEWERS[args.viewer]
    if args.harness and (args.identities is None or args.viewer_config is None):
        parser.error("--harness needs --identities and --viewer-config")
    if args.viewer == "desktop" and args.connection is None:
        parser.error("--connection is required for the desktop viewer")
    if args.viewer == "android" and args.harness:
        parser.error("--viewer android is a viewer of its own; --harness is the desktop viewer")
    for private in (args.connection, args.credential, args.server_key, args.identities):
        if private is None:
            continue
        if not private.is_file() or private.stat().st_mode & 0o077:
            parser.error(f"{private} must be a private file with mode 600")
    if not (1024 < args.port < 65536):
        parser.error("port must be an unprivileged loopback port")
    os.umask(0o077)
    # The native fixture image; scripts/fixture-smoke.sh tags an emulated build
    # for another architecture separately, so this is always the host's.
    image = args.image or f"wayland-vnc-{args.fixture}:dev"
    record = new_partial_record(
        commit=args.commit, target=args.fixture, viewer=viewer_name, versions={}
    )
    evidence_dir = args.evidence_root / args.fixture / viewer_name / record["run_id"]
    evidence_dir.mkdir(parents=True)
    common = {
        "fixture": args.fixture,
        "image": image,
        "container": f"wayland-vnc-{args.fixture}-qualify-{os.getpid()}",
        "port": args.port,
        "credential": args.credential.resolve(),
        "connection": args.connection.resolve() if args.connection else None,
        "log_dir": evidence_dir,
        "server_key": args.server_key.resolve() if args.server_key else None,
    }
    if args.viewer == "android" and args.kvm is not None:
        driver = KvmAndroidDriver(
            work=args.kvm.resolve(), viewer=android_viewer(args.sdk.resolve()), **common
        )
    elif args.viewer == "android":
        driver = AndroidDriver(viewer=android_viewer(args.sdk.resolve()), **common)
    elif args.kvm is not None and args.harness:
        driver = KvmHarnessDriver(
            work=args.kvm.resolve(),
            identities=args.identities.resolve(),
            viewer_config=args.viewer_config.resolve(),
            **common,
        )
    elif args.kvm is not None:
        driver = KvmDriver(work=args.kvm.resolve(), **common)
    elif args.harness:
        driver = HarnessDriver(
            identities=args.identities.resolve(),
            viewer_config=args.viewer_config.resolve(),
            **common,
        )
    else:
        driver = DockerRealVncDriver(**common)
    only = frozenset(args.scenario) if args.scenario else None
    run = Session(fixture=args.fixture, evidence_dir=evidence_dir, driver=driver, only=only)
    try:
        # Inside the try: a start that fails half-way (the fixture container is up,
        # the harness is not) must still reach stop(), or the container is leaked.
        driver.start()
        if not driver.smoke(1920, 1080):
            raise SystemExit("fixture did not pass its smoke checks; see the evidence directory")
        outcomes = run_scenarios(run)
        # After the scenarios: the viewer's own banner is in its logs only once it
        # has connected, so gathering earlier could never record its version.
        record["versions"] = driver.versions()
    finally:
        driver.stop()
    record = fill_record(record, run, outcomes, args.evidence_root)
    errors = validate_record(record, args.commit, args.evidence_root)
    records_dir = args.evidence_root / "records"
    records_dir.mkdir(exist_ok=True)
    target = records_dir / f"{args.fixture}-{viewer_name}-{record['run_id']}.json"
    target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "record": str(target),
        "status": record["status"],
        "scenarios": record["scenarios"],
        "first_frame_seconds": record["first_frame_seconds"],
        "reconnect_cycles": record["reconnect_cycles"],
        "validation_errors": errors,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    # The post-run smoke is a required part of the verdict: a fixture that died
    # during the run proves nothing, whatever its scenarios recorded.
    healthy = record.get("post_run_smoke") == "passed"
    acceptable = healthy and all(s in ("passed", "not-run") for s in record["scenarios"].values())
    raise SystemExit(0 if acceptable else 1)


if __name__ == "__main__":
    main()
