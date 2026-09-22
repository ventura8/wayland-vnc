"""In-container smoke checks for isolated compositor fixtures.

A passing smoke check proves only that a disposable fixture started correctly:
compositor, Wayland socket, WayVNC control socket and listener, native scene,
expected output mode, and no XWayland. It is never viewer compatibility evidence.
"""

import argparse
import ast
import json
import os
import pwd
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

SCENE_MARKER = "/fixture/scene.py"
VNC_PORT = 5900
TCP_LISTEN = "0A"
XWAYLAND_EXECUTABLES = (Path("/usr/bin/Xwayland"), Path("/usr/local/bin/Xwayland"))
XWAYLAND_STUB = Path("/fixture/labwc/Xwayland-disabled")
MAX_TIMEOUT = 120


@dataclass(frozen=True)
class Process:
    pid: int
    comm: str
    cmdline: tuple[str, ...]
    environ: dict[str, str]


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Profile:
    """What a genuine fixture session must contain beyond the compositor itself."""

    compositor: str
    session_process: str | None = None
    desktop_processes: tuple[str, ...] = ()
    desktop_hint: str | None = None
    server: str = "wayvnc"
    sockets: tuple[str, ...] = ("wayvncctl",)
    outputs_source: str = "wayvnc"
    requires_opengl: bool = False


FIXTURES = {
    "sway": Profile("sway"),
    "labwc": Profile("labwc"),
    "xfce-labwc": Profile(
        "labwc",
        "xfce4-session",
        ("xfce4-session", "xfsettingsd", "xfce4-panel", "xfdesktop"),
        "XFCE",
    ),
    "lxqt-labwc": Profile("labwc", "lxqt-session", ("lxqt-session", "lxqt-panel"), "LXQt"),
    "wayfire": Profile("wayfire"),
    "hyprland": Profile("Hyprland"),
    # Genuine GNOME path: Mutter ScreenCast/RemoteDesktop into the private GRD daemon.
    "gnome": Profile(
        "gnome-shell",
        "gnome-shell",
        ("gnome-shell", "pipewire", "wireplumber"),
        "GNOME",
        server="gnome-remote-desktop-daemon",
        sockets=("pipewire-0", "system_bus"),
        outputs_source="mutter",
    ),
    # Genuine KDE path: KWin screencast through the portals and PipeWire into TigerVNC.
    "plasma": Profile(
        "kwin_wayland",
        "plasmashell",
        ("plasmashell", "xdg-desktop-portal", "xdg-desktop-portal-kde", "pipewire", "wireplumber"),
        "KDE",
        server="w0vncserver",
        sockets=("pipewire-0",),
        outputs_source="kwin",
        requires_opengl=True,
    ),
}


def list_processes(proc_root: Path) -> list[Process]:
    """Snapshot /proc; processes that exit mid-read are simply omitted."""
    processes = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
            raw_cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
            raw_environ = (entry / "environ").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        environ = {}
        for item in raw_environ.split("\0"):
            key, separator, value = item.partition("=")
            if separator:
                environ[key] = value
        cmdline = tuple(part for part in raw_cmdline.split("\0") if part)
        processes.append(Process(int(entry.name), comm, cmdline, environ))
    return processes


def tcp_listeners(*tables: Path) -> set[int]:
    """Parse listening TCP ports from /proc/net/tcp style tables (IPv4 and IPv6)."""
    ports = set()
    for table in tables:
        if not table.exists():
            continue
        for line in table.read_text(encoding="utf-8").splitlines()[1:]:
            fields = line.split()
            if len(fields) > 3 and fields[3] == TCP_LISTEN:
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return ports


def wayvnc_outputs(control_socket: Path) -> list[dict]:
    result = subprocess.run(
        ["/usr/bin/wayvncctl", "--socket", str(control_socket), "--json", "output-list"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    outputs = json.loads(result.stdout)
    if not isinstance(outputs, list):
        raise ValueError("wayvncctl output-list did not return a list")
    return outputs


def _single(name: str, matches: list, what: str) -> Check:
    return Check(name, len(matches) == 1, f"{len(matches)} {what} process(es); expected exactly 1")


QEMU_USER_RE = re.compile(r"^qemu-[a-z0-9_]+(-static)?$")


def executable(cmdline: tuple[str, ...]) -> str | None:
    """The program a process runs, by name; under qemu user-mode emulation (a
    foreign-architecture container on a developer machine) argv[0] is the
    interpreter and the program is argv[1]."""
    if not cmdline:
        return None
    if QEMU_USER_RE.match(Path(cmdline[0]).name) and len(cmdline) > 1:
        return Path(cmdline[1]).name
    return Path(cmdline[0]).name


def named(processes: list[Process], name: str) -> list[Process]:
    """Match by executable name; /proc comm is truncated to fifteen characters."""
    return [
        p
        for p in processes
        if p.comm == name[:15] and (not p.cmdline or executable(p.cmdline) == name)
    ]


def _bus_owner(bus_address: str) -> str | None:
    """The account a session bus belongs to, from its socket path; None when the
    address names no path (an abstract socket) or the owner cannot be told."""
    for part in bus_address.split(";")[0].split(","):
        key, separator, value = part.partition("=")
        if separator and key == "unix:path":
            try:
                return pwd.getpwuid(os.stat(value).st_uid).pw_name
            except (OSError, KeyError):
                return None
    return None


def _session_bus_call(
    bus_address: str, argv: list[str], timeout: int
) -> subprocess.CompletedProcess:
    """Run a command against a fixture's session bus. In a KVM guest the checker runs
    as root (the fixture account cannot read its own processes' /proc), but the
    fixture's session bus admits only its own account, so the call is handed to the
    bus socket's owner."""
    owner = _bus_owner(bus_address)
    if os.geteuid() == 0 and owner not in (None, "root"):
        argv = ["/usr/sbin/runuser", "-u", owner, "--", *argv]
    return subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"DBUS_SESSION_BUS_ADDRESS": bus_address, "PATH": "/usr/bin"},
    )


def kwin_outputs(bus_address: str) -> list[dict]:
    """Read KWin's screen geometry and compositing type over the session bus."""
    if not bus_address:
        raise ValueError("The compositor process exposes no session bus address")
    result = _session_bus_call(
        bus_address,
        [
            "/usr/bin/gdbus",
            "call",
            "--session",
            "--dest",
            "org.kde.KWin",
            "--object-path",
            "/KWin",
            "--method",
            "org.kde.KWin.supportInformation",
        ],
        timeout=10,
    )
    reply = ast.literal_eval(result.stdout.strip())
    if not isinstance(reply, tuple) or len(reply) != 1 or not isinstance(reply[0], str):
        raise ValueError("Unexpected supportInformation reply")
    return parse_kwin_support(reply[0])


def mutter_outputs(bus_address: str) -> list[dict]:
    """Ask Mutter's DisplayConfig for the current modes through the fixture helper."""
    if not bus_address:
        raise ValueError("The compositor process exposes no session bus address")
    result = _session_bus_call(
        bus_address, ["/usr/bin/python3", "/fixture/gnome/mutter-outputs.py"], timeout=15
    )
    outputs = json.loads(result.stdout)
    if not isinstance(outputs, list):
        raise ValueError("mutter-outputs helper did not return a list")
    return outputs


def parse_kwin_support(text: str) -> list[dict]:
    """Extract the Screens and Compositing sections of KWin's supportInformation."""
    outputs: list[dict] = []
    compositing = None
    section = None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if index + 1 < len(lines) and lines[index + 1].startswith("==="):
            section = line.strip()
            continue
        key, separator, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not separator:
            continue
        if section == "Screens":
            if key == "Name":
                outputs.append({"name": value, "captured": True})
            elif key == "Enabled" and outputs:
                outputs[-1]["captured"] = value == "1"
            elif key == "Geometry" and outputs and "x" in value:
                width, _, height = value.rsplit(",", 1)[-1].partition("x")
                outputs[-1]["width"], outputs[-1]["height"] = int(width), int(height)
        elif section == "Compositing" and key == "Compositing Type":
            compositing = value
    for output in outputs:
        output["compositing"] = compositing
    return outputs


@dataclass(frozen=True)
class Expectation:
    fixture: str
    width: int
    height: int

    def __post_init__(self):
        if self.fixture not in FIXTURES:
            raise ValueError(f"Unsupported fixture: {self.fixture}")
        if self.width < 400 or self.height < 200:
            raise ValueError("Expected output mode is too small for a qualification scene")

    @property
    def profile(self) -> Profile:
        return FIXTURES[self.fixture]


@dataclass(frozen=True)
class Host:
    """Where the checker looks; overridable so unit tests never touch real /proc."""

    proc_root: Path = Path("/proc")
    runtime_dir: Path = Path("/tmp/wayland-vnc-runtime")
    tcp_table: Path = Path("/proc/net/tcp")
    tcp6_table: Path = Path("/proc/net/tcp6")
    outputs: Callable[[Path], list[dict]] = wayvnc_outputs
    kwin: Callable[[str], list[dict]] = kwin_outputs
    mutter: Callable[[str], list[dict]] = mutter_outputs
    xwayland_executables: tuple[Path, ...] = XWAYLAND_EXECUTABLES
    xwayland_stub: Path = XWAYLAND_STUB


CONTAINER = Host()


def _scene_environment(scenes: list[Process], sockets: list[Path]) -> Check:
    # Never echo the environment itself; report only which contract keys are wrong.
    scene_env = scenes[0].environ if len(scenes) == 1 else {}
    problems = []
    if scene_env.get("GDK_BACKEND") != "wayland":
        problems.append("GDK_BACKEND is not wayland")
    if not sockets or scene_env.get("WAYLAND_DISPLAY") != sockets[0].name:
        problems.append("WAYLAND_DISPLAY does not name the fixture socket")
    if "DISPLAY" in scene_env:
        problems.append("DISPLAY is set")
    return Check("scene-wayland-environment", not problems, "; ".join(problems) or "ok")


def _xwayland_executable(host: Host) -> Check:
    """Any Xwayland executable must be absent or the fixture's fail-closed stub."""
    try:
        stub = host.xwayland_stub.read_bytes()
    except OSError:
        stub = None
    usable = []
    for path in host.xwayland_executables:
        if path.exists() and (stub is None or path.read_bytes() != stub):
            usable.append(str(path))
    detail = ", ".join(usable) or "absent or fail-closed stub"
    return Check("no-usable-xwayland-executable", not usable, detail)


def _query_outputs(host: Host, profile: Profile, control: Path, compositor_env: dict) -> list:
    bus_address = compositor_env.get("DBUS_SESSION_BUS_ADDRESS", "")
    if profile.outputs_source == "kwin":
        return host.kwin(bus_address)
    if profile.outputs_source == "mutter":
        return host.mutter(bus_address)
    return host.outputs(control)


def _captured_output(
    host: Host, expected: Expectation, control: Path, compositor_env: dict
) -> Check:
    profile = expected.profile
    try:
        outputs = _query_outputs(host, profile, control, compositor_env)
        captured = [output for output in outputs if output.get("captured")]
        detail = ", ".join(
            f"{o.get('name')} {o.get('width')}x{o.get('height')} {o.get('compositing') or ''}"
            for o in captured
        ).strip()
    except (OSError, ValueError, subprocess.SubprocessError, ImportError) as error:
        captured, detail = [], f"output query failed: {type(error).__name__}"
    mode_ok = (
        len(captured) == 1
        and captured[0].get("width") == expected.width
        and captured[0].get("height") == expected.height
        and (not profile.requires_opengl or captured[0].get("compositing") == "OpenGL")
    )
    return Check("captured-output-mode", mode_ok, detail or "no captured output")


def _desktop_session(processes: list[Process], profile: Profile) -> list[Check]:
    """A desktop target needs its real session processes, not just a desktop name."""
    if profile.session_process is None:
        return []
    checks = []
    for name in profile.desktop_processes:
        checks.append(_single(f"desktop-process-{name}", named(processes, name), name))
    sessions = named(processes, profile.session_process)
    env = sessions[0].environ if len(sessions) == 1 else {}
    hint = env.get("XDG_CURRENT_DESKTOP", "")
    genuine = profile.desktop_hint in hint.split(":") and env.get("XDG_SESSION_TYPE") == "wayland"
    checks.append(Check("desktop-session-environment", genuine, f"XDG_CURRENT_DESKTOP={hint!r}"))
    return checks


def run_checks(expected: Expectation, host: Host | None = None) -> list[Check]:
    host = CONTAINER if host is None else host
    profile = expected.profile
    processes = list_processes(host.proc_root)
    compositors = named(processes, profile.compositor)
    checks = [_single("compositor-process", compositors, profile.compositor)]
    compositor_env = compositors[0].environ if len(compositors) == 1 else {}
    checks.extend(_desktop_session(processes, profile))
    sockets = sorted(p for p in host.runtime_dir.glob("wayland-*") if p.is_socket())
    checks.append(
        Check("wayland-socket", len(sockets) == 1, f"{len(sockets)} Wayland socket(s); expected 1")
    )
    for name in profile.sockets:
        path = host.runtime_dir / name
        checks.append(Check(f"runtime-socket-{name}", path.is_socket(), path.name))
    control = host.runtime_dir / "wayvncctl"
    server = named(processes, profile.server)
    checks.append(_single(f"server-process-{profile.server}", server, profile.server))
    listening = VNC_PORT in tcp_listeners(host.tcp_table, host.tcp6_table)
    checks.append(Check("vnc-listener", listening, f"tcp port {VNC_PORT} listening: {listening}"))
    scenes = [p for p in processes if SCENE_MARKER in p.cmdline]
    checks.append(_single("native-scene-process", scenes, "native scene"))
    checks.append(_scene_environment(scenes, sockets))
    xwayland = [p for p in processes if p.comm.lower() == "xwayland"]
    checks.append(
        Check("no-xwayland-process", not xwayland, f"{len(xwayland)} Xwayland process(es)")
    )
    checks.append(_xwayland_executable(host))
    checks.append(_captured_output(host, expected, control, compositor_env))
    return checks


def wait_for_checks(
    expected: Expectation,
    host: Host | None = None,
    *,
    timeout: float = 30,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """Poll bounded by timeout; a fixture that never settles fails, never skips."""
    if not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"Timeout must be within (0, {MAX_TIMEOUT}] seconds")
    # Resolved here, not in the signature: a test may replace this module's `time`
    # with a fake clock, and a default bound at definition time would ignore it.
    clock = time.monotonic if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    started = clock()
    deadline = started + timeout
    while True:
        checks = run_checks(expected, host)
        passed = all(check.passed for check in checks)
        if passed or clock() >= deadline:
            return {
                "schema_version": 1,
                "fixture": expected.fixture,
                "compositor": expected.profile.compositor,
                "passed": passed,
                "elapsed_seconds": round(clock() - started, 3),
                "checks": [asdict(check) for check in checks],
            }
        sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", choices=tuple(FIXTURES), required=True)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    expected = Expectation(args.fixture, args.width, args.height)
    report = wait_for_checks(expected, timeout=args.timeout)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
