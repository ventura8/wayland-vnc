"""Fake /proc trees test the fixture smoke checker; they prove no fixture works."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from wayland_vnc import fixture_smoke
from wayland_vnc.fixture_smoke import (
    Expectation,
    Host,
    list_processes,
    run_checks,
    tcp_listeners,
    wait_for_checks,
)

SCENE_ENV = {"GDK_BACKEND": "wayland", "WAYLAND_DISPLAY": "wayland-0"}
OUTPUTS = [{"name": "HEADLESS-1", "width": 1920, "height": 1080, "captured": True}]


def fake_process(proc_root: Path, pid: int, comm: str, cmdline: list[str], environ: dict):
    entry = proc_root / str(pid)
    entry.mkdir()
    (entry / "comm").write_text(comm + "\n", encoding="utf-8")
    (entry / "cmdline").write_bytes("\0".join(cmdline).encode() + b"\0")
    (entry / "environ").write_bytes(
        b"".join(f"{key}={value}\0".encode() for key, value in environ.items())
    )


def bind_socket(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    return listener


@pytest.fixture(name="healthy")
def healthy_fixture(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "self").mkdir()
    fake_process(proc_root, 10, "labwc", ["/usr/bin/labwc"], {})
    fake_process(proc_root, 15, "python3", ["/usr/bin/python3", "/fixture/scene.py"], SCENE_ENV)
    fake_process(proc_root, 17, "wayvnc", ["/usr/bin/wayvnc"], {})
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    sockets = [bind_socket(runtime / "wayland-0"), bind_socket(runtime / "wayvncctl")]
    (runtime / "wayland-0.lock").touch()
    table = tmp_path / "tcp"
    table.write_text(
        "  sl  local_address rem_address   st\n"
        "   0: 00000000:170C 00000000:0000 0A extra fields\n"
        "   1: 0100007F:1F90 00000000:0000 01 extra fields\n",
        encoding="utf-8",
    )
    host = Host(
        proc_root=proc_root,
        runtime_dir=runtime,
        tcp_table=table,
        tcp6_table=tmp_path / "tcp6-missing",
        outputs=lambda _control: OUTPUTS,
        xwayland_executables=(tmp_path / "Xwayland",),
        xwayland_stub=tmp_path / "Xwayland-disabled",
    )
    yield host
    for listener in sockets:
        listener.close()


def failures(checks):
    return {check.name: check.detail for check in checks if not check.passed}


def test_healthy_fixture_passes_every_check(healthy):
    checks = run_checks(Expectation("labwc", 1920, 1080), healthy)
    assert failures(checks) == {}
    assert [check.name for check in checks] == [
        "compositor-process",
        "wayland-socket",
        "runtime-socket-wayvncctl",
        "server-process-wayvnc",
        "vnc-listener",
        "native-scene-process",
        "scene-wayland-environment",
        "no-xwayland-process",
        "no-usable-xwayland-executable",
        "captured-output-mode",
    ]


def test_wrong_compositor_and_mode_fail(healthy):
    checks = run_checks(Expectation("sway", 1280, 720), healthy)
    assert set(failures(checks)) == {"compositor-process", "captured-output-mode"}


def test_x11_leakage_is_detected(healthy, tmp_path):
    fake_process(healthy.proc_root, 20, "Xwayland", ["/usr/bin/Xwayland"], {})
    (tmp_path / "Xwayland").write_text("", encoding="utf-8")
    scene = healthy.proc_root / "15" / "environ"
    scene.write_bytes(scene.read_bytes() + b"DISPLAY=:0\0")
    problems = failures(run_checks(Expectation("labwc", 1920, 1080), healthy))
    assert problems["no-xwayland-process"] == "1 Xwayland process(es)"
    assert problems["no-usable-xwayland-executable"] == str(tmp_path / "Xwayland")
    assert problems["scene-wayland-environment"] == "DISPLAY is set"


def test_fail_closed_stub_is_not_a_usable_xwayland(healthy, tmp_path):
    (tmp_path / "Xwayland-disabled").write_bytes(b"#!/bin/sh\nexit 78\n")
    (tmp_path / "Xwayland").write_bytes(b"#!/bin/sh\nexit 78\n")
    assert failures(run_checks(Expectation("labwc", 1920, 1080), healthy)) == {}
    (tmp_path / "Xwayland").write_bytes(b"ELF real server")
    problems = failures(run_checks(Expectation("labwc", 1920, 1080), healthy))
    assert set(problems) == {"no-usable-xwayland-executable"}


def test_desktop_targets_require_genuine_session_processes(healthy):
    problems = failures(run_checks(Expectation("xfce-labwc", 1920, 1080), healthy))
    assert set(problems) == {
        "desktop-process-xfce4-session",
        "desktop-process-xfsettingsd",
        "desktop-process-xfce4-panel",
        "desktop-process-xfdesktop",
        "desktop-session-environment",
    }
    session_env = {"XDG_CURRENT_DESKTOP": "XFCE", "XDG_SESSION_TYPE": "wayland"}
    fake_process(healthy.proc_root, 22, "xfce4-session", ["xfce4-session"], session_env)
    for pid, name in ((34, "xfsettingsd"), (35, "xfce4-panel"), (37, "xfdesktop")):
        fake_process(healthy.proc_root, pid, name, [name], {})
    assert failures(run_checks(Expectation("xfce-labwc", 1920, 1080), healthy)) == {}
    lxqt_env = {"XDG_CURRENT_DESKTOP": "LXQt:wlroots"}
    fake_process(healthy.proc_root, 40, "lxqt-session", ["lxqt-session"], lxqt_env)
    fake_process(healthy.proc_root, 41, "lxqt-panel", ["lxqt-panel"], {})
    problems = failures(run_checks(Expectation("lxqt-labwc", 1920, 1080), healthy))
    assert problems == {"desktop-session-environment": "XDG_CURRENT_DESKTOP='LXQt:wlroots'"}


def test_missing_scene_socket_and_listener_fail(healthy):
    (healthy.proc_root / "15" / "cmdline").write_bytes(b"/usr/bin/python3\0")
    (healthy.runtime_dir / "wayland-0").unlink()
    healthy.tcp_table.write_text("header only\n", encoding="utf-8")
    problems = failures(run_checks(Expectation("labwc", 1920, 1080), healthy))
    assert set(problems) == {
        "wayland-socket",
        "vnc-listener",
        "native-scene-process",
        "scene-wayland-environment",
    }
    assert "WAYLAND_DISPLAY" in problems["scene-wayland-environment"]


def test_output_query_failure_is_a_failed_check_not_a_skip(healthy):
    def broken(_control):
        raise ValueError("no control socket")

    host = Host(
        proc_root=healthy.proc_root,
        runtime_dir=healthy.runtime_dir,
        tcp_table=healthy.tcp_table,
        outputs=broken,
        xwayland_executables=healthy.xwayland_executables,
    )
    problems = failures(run_checks(Expectation("labwc", 1920, 1080), host))
    assert problems == {"captured-output-mode": "output query failed: ValueError"}


def test_uncaptured_outputs_do_not_count(healthy):
    host = Host(
        proc_root=healthy.proc_root,
        runtime_dir=healthy.runtime_dir,
        tcp_table=healthy.tcp_table,
        outputs=lambda _control: [dict(OUTPUTS[0], captured=False)],
        xwayland_executables=healthy.xwayland_executables,
    )
    problems = failures(run_checks(Expectation("labwc", 1920, 1080), host))
    assert problems == {"captured-output-mode": "no captured output"}


def test_plasma_profile_uses_kwin_outputs_and_requires_opengl(healthy, tmp_path):
    support = (
        "Platform\n========\nName: KWin::VirtualBackend\n\n"
        "Screens\n=======\nNumber of Screens: 1\n\nScreen 0:\n---------\nName: Virtual-0\n"
        "Enabled: 1\nGeometry: 0,0,1920x1080\nScale: 1\n\nCompositing\n===========\n"
        "Compositing Type: QPainter\n"
    )
    seen = []

    def kwin(address):
        seen.append(address)
        return fixture_smoke.parse_kwin_support(support)

    host = Host(
        proc_root=healthy.proc_root,
        runtime_dir=healthy.runtime_dir,
        tcp_table=healthy.tcp_table,
        outputs=lambda _control: pytest.fail("wayvnc outputs must not be queried"),
        kwin=kwin,
        xwayland_executables=healthy.xwayland_executables,
        xwayland_stub=healthy.xwayland_stub,
    )
    env = {"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "wayland"}
    fake_process(
        healthy.proc_root,
        14,
        "kwin_wayland",
        ["/usr/bin/kwin_wayland"],
        {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"},
    )
    fake_process(healthy.proc_root, 77, "plasmashell", ["/usr/bin/plasmashell"], env)
    fake_process(healthy.proc_root, 22, "xdg-desktop-por", ["/usr/libexec/xdg-desktop-portal"], {})
    fake_process(
        healthy.proc_root, 41, "xdg-desktop-por", ["/usr/lib/libexec/xdg-desktop-portal-kde"], {}
    )
    fake_process(healthy.proc_root, 11, "pipewire", ["/usr/bin/pipewire"], {})
    fake_process(healthy.proc_root, 13, "wireplumber", ["/usr/bin/wireplumber"], {})
    fake_process(
        healthy.proc_root, 79, "w0vncserver", ["/opt/wayland-vnc/tigervnc/bin/w0vncserver"], {}
    )
    bind = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bind.bind(str(healthy.runtime_dir / "pipewire-0"))
    try:
        problems = failures(run_checks(Expectation("plasma", 1920, 1080), host))
    finally:
        bind.close()
    assert seen == ["unix:path=/x"]
    assert problems == {"captured-output-mode": "Virtual-0 1920x1080 QPainter"}
    parsed = fixture_smoke.parse_kwin_support(support.replace("QPainter", "OpenGL"))
    assert parsed == [
        {
            "name": "Virtual-0",
            "captured": True,
            "width": 1920,
            "height": 1080,
            "compositing": "OpenGL",
        }
    ]


def test_kwin_outputs_uses_gdbus_with_the_compositor_bus(monkeypatch):
    calls = []

    class Result:
        stdout = "('Screens\\n=======\\nName: Virtual-0\\nEnabled: 1\\nGeometry: 0,0,1920x1080\\n"
        stdout += "Compositing\\n===========\\nCompositing Type: OpenGL\\n',)\n"

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(fixture_smoke.subprocess, "run", fake_run)
    outputs = fixture_smoke.kwin_outputs("unix:path=/bus")
    assert outputs[0]["width"] == 1920
    assert outputs[0]["compositing"] == "OpenGL"
    args, kwargs = calls[0]
    assert args[0] == "/usr/bin/gdbus"
    assert "org.kde.KWin.supportInformation" in args
    assert kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/bus"
    assert kwargs["timeout"] == 10
    Result.stdout = "42\n"
    with pytest.raises(ValueError):
        fixture_smoke.kwin_outputs("unix:path=/bus")
    with pytest.raises(ValueError):
        fixture_smoke.kwin_outputs("")


def test_gnome_profile_uses_the_mutter_helper(healthy, monkeypatch):
    seen = []
    host = Host(
        proc_root=healthy.proc_root,
        runtime_dir=healthy.runtime_dir,
        tcp_table=healthy.tcp_table,
        tcp6_table=healthy.tcp6_table,
        outputs=lambda _control: pytest.fail("wayvnc outputs must not be queried"),
        mutter=lambda address: seen.append(address) or OUTPUTS,
        xwayland_executables=healthy.xwayland_executables,
        xwayland_stub=healthy.xwayland_stub,
    )
    env = {"XDG_CURRENT_DESKTOP": "GNOME", "XDG_SESSION_TYPE": "wayland"}
    bus = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/g", **env}
    fake_process(healthy.proc_root, 14, "gnome-shell", ["/usr/bin/gnome-shell"], bus)
    fake_process(healthy.proc_root, 11, "pipewire", ["/usr/bin/pipewire"], {})
    fake_process(healthy.proc_root, 13, "wireplumber", ["/usr/bin/wireplumber"], {})
    daemon = "/opt/wayland-vnc/grd/libexec/gnome-remote-desktop-daemon"
    fake_process(healthy.proc_root, 440, "gnome-remote-de", [daemon], {})
    sockets = [bind_socket(healthy.runtime_dir / name) for name in ("pipewire-0", "system_bus")]
    try:
        problems = failures(run_checks(Expectation("gnome", 1920, 1080), host))
    finally:
        for listener in sockets:
            listener.close()
    assert problems == {}
    assert seen == ["unix:path=/g"]

    calls = []

    class Result:
        stdout = json.dumps(OUTPUTS)

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(fixture_smoke.subprocess, "run", fake_run)
    assert fixture_smoke.mutter_outputs("unix:path=/g") == OUTPUTS
    assert calls[0][0][-1] == "/fixture/gnome/mutter-outputs.py"
    assert calls[0][1]["env"]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/g"
    Result.stdout = "{}"
    with pytest.raises(ValueError):
        fixture_smoke.mutter_outputs("unix:path=/g")
    with pytest.raises(ValueError):
        fixture_smoke.mutter_outputs("")


def test_named_matches_truncated_comm_by_executable(tmp_path):
    fake_process(tmp_path, 1, "xdg-desktop-por", ["/usr/libexec/xdg-desktop-portal"], {})
    fake_process(tmp_path, 2, "xdg-desktop-por", ["/usr/lib/libexec/xdg-desktop-portal-kde"], {})
    processes = list_processes(tmp_path)
    assert [p.pid for p in fixture_smoke.named(processes, "xdg-desktop-portal-kde")] == [2]
    assert [p.pid for p in fixture_smoke.named(processes, "xdg-desktop-portal")] == [1]


def test_named_sees_through_the_qemu_user_interpreter(tmp_path):
    """In a foreign-architecture container on a developer machine, argv[0] of every
    process is qemu-<arch>; the program is argv[1]. comm still names the program."""
    fake_process(
        tmp_path, 1, "sway", ["/usr/bin/qemu-aarch64", "/usr/bin/sway", "--config", "x"], {}
    )
    fake_process(tmp_path, 2, "wayvnc", ["/usr/bin/qemu-aarch64-static", "/usr/bin/wayvnc"], {})
    fake_process(tmp_path, 3, "qemu-aarch64", ["/usr/bin/qemu-aarch64"], {})
    processes = list_processes(tmp_path)
    assert [p.pid for p in fixture_smoke.named(processes, "sway")] == [1]
    assert [p.pid for p in fixture_smoke.named(processes, "wayvnc")] == [2]
    assert fixture_smoke.executable(()) is None
    assert fixture_smoke.executable(("/usr/bin/qemu-aarch64",)) == "qemu-aarch64"


def test_expectation_rejects_unknown_compositor_and_tiny_modes():
    with pytest.raises(ValueError):
        Expectation("xorg", 1920, 1080)
    with pytest.raises(ValueError):
        Expectation("sway", 320, 200)


def test_vanished_processes_are_skipped_not_fatal(tmp_path):
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "comm").write_text("init\n", encoding="utf-8")
    (tmp_path / "2").mkdir()
    (tmp_path / "2" / "comm").write_text("gone\n", encoding="utf-8")
    (tmp_path / "2" / "cmdline").write_bytes(b"gone\0")
    (tmp_path / "2" / "environ").write_bytes(b"NOEQUALS\0A=1\0")
    processes = list_processes(tmp_path)
    assert [(p.pid, p.comm, p.cmdline, p.environ) for p in processes] == [
        (2, "gone", ("gone",), {"A": "1"})
    ]


def test_tcp_listener_parsing_ignores_non_listening_rows(tmp_path):
    table = tmp_path / "tcp"
    table.write_text(
        "header\n 0: 00000000:170C 00000000:0000 0A\n 1: 00000000:0016 00000000:0000 01\n",
        encoding="utf-8",
    )
    assert tcp_listeners(table) == {5900}
    table6 = tmp_path / "tcp6"
    table6.write_text(
        "header\n 0: " + "0" * 32 + ":170D " + "0" * 32 + ":0000 0A\n",
        encoding="utf-8",
    )
    assert tcp_listeners(table, table6, tmp_path / "absent") == {5900, 5901}


def test_wait_polls_until_pass_and_reports_elapsed(healthy):
    scene = healthy.proc_root / "15" / "cmdline"
    original = scene.read_bytes()
    scene.write_bytes(b"/usr/bin/python3\0")
    ticks = iter([0.0, 0.0, 1.0])
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        scene.write_bytes(original)

    report = wait_for_checks(
        Expectation("labwc", 1920, 1080),
        healthy,
        timeout=5,
        clock=lambda: next(ticks),
        sleep=sleep,
    )
    assert report["passed"] is True
    assert slept == [0.5]
    assert report["elapsed_seconds"] == 1.0
    assert report["schema_version"] == 1


def test_wait_fails_closed_at_deadline(healthy):
    (healthy.runtime_dir / "wayvncctl").unlink()
    ticks = iter([0.0, 10.0, 10.0])
    report = wait_for_checks(
        Expectation("labwc", 1920, 1080),
        healthy,
        timeout=5,
        clock=lambda: next(ticks),
        sleep=lambda _seconds: pytest.fail("must not sleep after the deadline"),
    )
    assert report["passed"] is False
    assert {c["name"] for c in report["checks"] if not c["passed"]} == {"runtime-socket-wayvncctl"}


def test_timeout_bounds():
    expectation = Expectation("sway", 1920, 1080)
    with pytest.raises(ValueError):
        wait_for_checks(expectation, timeout=0)
    with pytest.raises(ValueError):
        wait_for_checks(expectation, timeout=fixture_smoke.MAX_TIMEOUT + 1)


def test_main_reports_json_and_exit_status(healthy, monkeypatch, capsys):
    monkeypatch.setattr(fixture_smoke, "CONTAINER", healthy)
    assert fixture_smoke.main(["--fixture", "labwc", "--timeout", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["fixture"] == "labwc"
    assert report["compositor"] == "labwc"
    # Only the module under test gets the fake clock: patching time.monotonic itself
    # with an exhaustible iterator would hand StopIteration to anything else in the
    # process that looked at the clock during this test.
    fake_time = SimpleNamespace(monotonic=iter([0.0, 5.0, 5.0]).__next__, sleep=lambda _s: None)
    monkeypatch.setattr(fixture_smoke, "time", fake_time)
    assert fixture_smoke.main(["--fixture", "sway", "--timeout", "1"]) == 1


def test_wayvnc_outputs_uses_bounded_control_query(monkeypatch, tmp_path):
    calls = []

    class Result:
        stdout = json.dumps(OUTPUTS)

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(fixture_smoke.subprocess, "run", fake_run)
    assert fixture_smoke.wayvnc_outputs(tmp_path / "wayvncctl") == OUTPUTS
    args, kwargs = calls[0]
    assert args[0] == "/usr/bin/wayvncctl"
    assert str(tmp_path / "wayvncctl") in args
    assert kwargs["timeout"] == 10
    assert kwargs["check"] is True
    Result.stdout = json.dumps({"not": "a list"})
    with pytest.raises(ValueError):
        fixture_smoke.wayvnc_outputs(tmp_path / "wayvncctl")


def test_mutter_helper_runs_as_the_bus_owner_when_the_checker_is_root(monkeypatch, tmp_path):
    """A KVM guest runs the checker as root; the fixture's session bus admits only
    its own account, so the helper is handed to the socket's owner there."""
    socket_path = tmp_path / "bus"
    socket_path.write_text("")
    me = fixture_smoke.pwd.getpwuid(fixture_smoke.os.getuid()).pw_name
    address = f"unix:path={socket_path},guid=abc"
    assert fixture_smoke._bus_owner(address) == me
    assert fixture_smoke._bus_owner("unix:abstract=/tmp/dbus-x,guid=abc") is None
    assert fixture_smoke._bus_owner("unix:path=/nowhere/at/all") is None
    calls = []

    class Result:
        stdout = json.dumps(OUTPUTS)

    monkeypatch.setattr(
        fixture_smoke.subprocess, "run", lambda args, **kw: calls.append(args) or Result()
    )
    monkeypatch.setattr(fixture_smoke.os, "geteuid", lambda: 0)
    fixture_smoke.mutter_outputs(address)
    assert calls[-1][:4] == ["/usr/sbin/runuser", "-u", me, "--"]
    # Not root: the helper runs directly, as in a container.
    monkeypatch.setattr(fixture_smoke.os, "geteuid", lambda: 1000)
    fixture_smoke.mutter_outputs(address)
    assert calls[-1][0] == "/usr/bin/python3"


def test_kwin_geometry_is_scaled_to_the_output_mode():
    support = (
        "Screens\n=======\nName: Virtual-1\nEnabled: 1\nGeometry: 0,0,1920x1080\n"
        "Scale: 2\nName: Virtual-2\nEnabled: 0\n"
    )
    outputs = fixture_smoke.parse_kwin_support(support)
    assert (outputs[0]["width"], outputs[0]["height"]) == (3840, 2160)
    assert not outputs[1]["captured"]
