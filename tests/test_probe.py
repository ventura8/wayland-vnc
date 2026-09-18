"""Capability tests deliberately distinguish capture from remote input."""

import io
import runpy
import subprocess

import pytest

from wayland_vnc import cli, probe


def capabilities(**overrides):
    result = {
        "session_type": "wayland",
        "interfaces": [],
        "gnome_remote_desktop": False,
        "gnome_screencast": False,
        "kwin": False,
        "remote_desktop_portal": False,
        "screencast_portal": False,
        "binaries": {},
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize("session", ["x11", "unknown", ""])
def test_reject_non_wayland(session):
    assert probe.select_backend(capabilities(session_type=session))[0] is None


def test_gnome_requires_both_interfaces():
    assert probe.select_backend(capabilities(gnome_remote_desktop=True))[0] is None
    assert (
        probe.select_backend(capabilities(gnome_remote_desktop=True, gnome_screencast=True))[0]
        == "grd"
    )


def test_kwin_requires_real_portals():
    assert probe.select_backend(capabilities(kwin=True))[0] is None
    assert (
        probe.select_backend(
            capabilities(kwin=True, remote_desktop_portal=True, screencast_portal=True)
        )[0]
        == "w0vncserver"
    )


@pytest.mark.parametrize(
    "capture",
    [
        ["zwlr_screencopy_manager_v1"],
        ["ext_image_copy_capture_manager_v1", "ext_output_image_capture_source_manager_v1"],
    ],
)
def test_wayvnc_requires_capture_and_input(capture):
    assert probe.select_backend(capabilities(interfaces=capture))[0] is None
    inputs = ["zwlr_virtual_pointer_manager_v1", "zwp_virtual_keyboard_manager_v1"]
    assert probe.select_backend(capabilities(interfaces=inputs))[0] is None
    assert probe.select_backend(capabilities(interfaces=capture + inputs))[0] == "wayvnc"


def test_no_binaries_or_environment():
    result = probe.collect({}, which=lambda _: None)
    assert result["session_type"] == "unknown"
    assert not result["interfaces"]
    assert not result["remote_desktop_portal"]


def test_collect_existing_portal_and_protocols():
    def run(args):
        if args == ["/bin/wayland-info"]:
            return "interface: 'zwlr_screencopy_manager_v1', version: 1"
        if "call" in args:
            return "['org.freedesktop.portal.Desktop', 'org.kde.KWin']"
        return (
            "interface org.freedesktop.portal.RemoteDesktop { }; "
            "interface org.freedesktop.portal.ScreenCast { };"
        )

    result = probe.collect({"XDG_SESSION_TYPE": "wayland"}, run, lambda n: f"/bin/{n}")
    assert result["interfaces"] == ["zwlr_screencopy_manager_v1"]
    assert result["remote_desktop_portal"]
    assert result["kwin"]


def test_probe_does_not_activate_absent_portal():
    calls = []

    def run(args):
        calls.append(args)
        return "[]"

    probe.collect({}, run, lambda n: f"/bin/{n}")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [OSError(), subprocess.TimeoutExpired("probe", 5)])
def test_command_handles_missing_or_timeout(monkeypatch, failure):
    def run(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", run)
    assert probe.command(["missing"]) == ""


@pytest.mark.parametrize("code,expected", [(0, "ok"), (1, "")])
def test_command_exit_status(monkeypatch, code, expected):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, code, "ok", "")
    )
    assert probe.command(["probe"]) == expected


@pytest.mark.parametrize("action", ["doctor", "status"])
@pytest.mark.parametrize("output", [[], ["--json"]])
def test_cli_diagnostics_never_change_host(monkeypatch, capsys, action, output):
    monkeypatch.setattr(
        cli, "collect", lambda: capabilities(gnome_remote_desktop=True, gnome_screencast=True)
    )
    assert cli.main([action, *output]) == 0
    assert "unqualified" in capsys.readouterr().out


@pytest.mark.parametrize("action", ["start", "stop"])
def test_cli_start_stop_only_point_at_the_user_service(capsys, action):
    # The installed service is a systemd --user unit; start/stop print guidance and
    # never touch the host directly from the CLI.
    assert cli.main([action]) == 0
    assert "systemctl --user" in capsys.readouterr().out


def test_cli_set_password_and_provision(monkeypatch, tmp_path, capsys):
    # `set-password` syncs into the host's real backend; never do that from a test.
    monkeypatch.setattr(cli.runtime, "sync_backend_password", lambda *a, **k: None)
    monkeypatch.setenv(cli.runtime.CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "hunter2x")
    assert cli.main(["set-password"]) == 0
    assert "Stored viewer credential" in capsys.readouterr().out


def test_cli_set_password_stdin(monkeypatch, tmp_path, capsys):
    # `set-password` syncs into the host's real backend; never do that from a test.
    monkeypatch.setattr(cli.runtime, "sync_backend_password", lambda *a, **k: None)
    monkeypatch.setenv(cli.runtime.CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("piped-pw\npiped-pw\n"))
    assert cli.main(["set-password", "--stdin"]) == 0
    assert "Stored viewer credential" in capsys.readouterr().out
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    assert cli.main(["set-password", "--stdin", "--json"]) == 2
    assert "stdin ended" in capsys.readouterr().out
    monkeypatch.setattr(cli.runtime, "default_key_generator", lambda path: path.write_text("k"))
    assert cli.main(["provision", "--json"]) == 0
    assert "wayvnc.conf" in capsys.readouterr().out


def test_cli_serve_self_provisions_without_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli.runtime,
        "collect",
        lambda: capabilities(
            interfaces=[
                "zwlr_screencopy_manager_v1",
                "zwlr_virtual_pointer_manager_v1",
                "zwp_virtual_keyboard_manager_v1",
            ]
        ),
    )
    """No password stored: serve generates one and still reaches exec, never a refusal."""
    monkeypatch.setenv(cli.runtime.CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/wayvnc")
    monkeypatch.setattr(cli.runtime, "default_key_generator", lambda path: path.write_text("k"))
    execed = {}
    # The real execv would replace the test process with wayvnc; never let it run.
    monkeypatch.setattr(cli.os, "execv", lambda path, argv: execed.update(path=path, argv=argv))
    cli.main(["serve"])
    assert execed["path"] == "/usr/bin/wayvnc"
    assert (tmp_path / cli.runtime.CREDENTIALS_NAME).exists()


def test_cli_serve_execs_when_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli.runtime,
        "collect",
        lambda: capabilities(
            interfaces=[
                "zwlr_screencopy_manager_v1",
                "zwlr_virtual_pointer_manager_v1",
                "zwp_virtual_keyboard_manager_v1",
            ]
        ),
    )
    monkeypatch.setenv(cli.runtime.CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "hunter2x")
    cli.main(["set-password"])
    monkeypatch.setattr(cli.runtime, "default_key_generator", lambda path: path.write_text("k"))
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/wayvnc")
    execed = {}
    monkeypatch.setattr(cli.os, "execv", lambda path, argv: execed.update(path=path, argv=argv))
    cli.main(["serve"])
    assert execed["path"] == "/usr/bin/wayvnc"


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_staging_round_trip(tmp_path, capsys, as_json):
    root = tmp_path / "root"
    output = ["--json"] if as_json else []
    assert cli.main(["setup", "--backend", "wayvnc", "--staging-root", str(root), *output]) == 0
    rendered = capsys.readouterr().out
    assert ("manifest" if as_json else "staged") in rendered
    assert cli.main(["uninstall", "--staging-root", str(root), *output]) == 0
    assert "removed" in capsys.readouterr().out


def test_cli_staging_error(tmp_path, capsys):
    missing = tmp_path / "missing"
    assert cli.main(["uninstall", "--staging-root", str(missing), "--json"]) == 2
    assert "error" in capsys.readouterr().out


def test_cli_unsupported(monkeypatch, capsys):
    monkeypatch.setattr(cli, "collect", capabilities)
    assert cli.main(["doctor"]) == 2
    assert "none" in capsys.readouterr().out


def test_environment_default(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert probe.collect(which=lambda _: None)["session_type"] == "x11"


def test_module_entrypoint(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda: 0)
    with pytest.raises(SystemExit, match="0"):
        runpy.run_module("wayland_vnc", run_name="__main__")


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"XDG_SESSION_TYPE": "wayland"}, "wayland"),
        # Inside a systemd user unit the session type is often not imported, but the
        # Wayland display is; the installed unit is gated on exactly that variable.
        ({"WAYLAND_DISPLAY": "wayland-0"}, "wayland"),
        ({"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "wayland-0"}, "x11"),
        ({"XDG_SESSION_TYPE": "tty"}, "tty"),
        ({}, "unknown"),
    ],
)
def test_session_type_takes_a_wayland_display_as_evidence_when_the_type_is_unset(env, expected):
    assert probe.session_type(env) == expected


def test_collect_probes_protocols_inside_a_unit_that_only_exports_the_display():
    """The bug this guards: serve inside the unit selected no backend and refused,
    because the probe insisted on XDG_SESSION_TYPE, which the unit never sees."""
    ran = []

    def run(args):
        ran.append(args)
        return "interface: 'zwlr_screencopy_manager_v1', version: 3, name: 1\n"

    caps = probe.collect(
        env={"WAYLAND_DISPLAY": "wayland-0"},
        run=run,
        which=lambda name: "/usr/bin/wayland-info" if name == "wayland-info" else None,
    )
    assert caps["session_type"] == "wayland"
    assert ran and ran[0] == ["/usr/bin/wayland-info"]
    assert "zwlr_screencopy_manager_v1" in caps["interfaces"]
