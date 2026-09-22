"""Runtime provisioning/serving tests with injected filesystem and process boundaries.

No real WayVNC, compositor, or OpenSSL runs here; these lock the config contract,
credential permissions, local-network default, and fail-closed serving guards.
"""

import dataclasses
import stat
import subprocess

import pytest

from wayland_vnc import runtime

WLR = {
    "session_type": "wayland",
    "interfaces": [
        "zwlr_screencopy_manager_v1",
        "zwlr_virtual_pointer_manager_v1",
        "zwp_virtual_keyboard_manager_v1",
    ],
    "gnome_remote_desktop": False,
    "gnome_screencast": False,
    "kwin": False,
    "remote_desktop_portal": False,
    "screencast_portal": False,
    "binaries": {},
}
GNOME = {**WLR, "interfaces": [], "gnome_remote_desktop": True, "gnome_screencast": True}


def fake_key(path):
    path.write_text("-----BEGIN RSA PRIVATE KEY-----\n", encoding="utf-8")


def test_config_dir_prefers_override_then_xdg(tmp_path):
    assert runtime.config_dir({runtime.CONFIG_DIR_ENV: str(tmp_path / "o")}) == tmp_path / "o"
    xdg = runtime.config_dir({"XDG_CONFIG_HOME": str(tmp_path / "x")})
    assert xdg == tmp_path / "x" / "wayland-vnc"


def test_credentials_validation():
    with pytest.raises(ValueError, match="Username"):
        runtime.Credentials("", "secret1")
    with pytest.raises(ValueError, match="Password"):
        runtime.Credentials("vnc", "short")
    with pytest.raises(ValueError, match="Password"):
        runtime.Credentials("vnc", "nønascii-password")
    assert runtime.Credentials("vnc", "secret1").username == "vnc"


def test_set_password_writes_mode_600_and_matches(tmp_path):
    secrets = iter(["hunter2x", "hunter2x"])
    path = runtime.set_password(tmp_path, read_secret=lambda _prompt: next(secrets))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    creds = runtime.read_credentials(tmp_path)
    assert creds.username == "vnc"
    assert creds.password == "hunter2x"
    mismatch = iter(["hunter2x", "different"])
    with pytest.raises(ValueError, match="did not match"):
        runtime.set_password(tmp_path, read_secret=lambda _prompt: next(mismatch))


def test_read_credentials_missing_and_malformed(tmp_path):
    assert runtime.read_credentials(tmp_path) is None
    (tmp_path / runtime.CREDENTIALS_NAME).write_text("username=vnc\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        runtime.read_credentials(tmp_path)


def test_provision_binds_loopback_by_default_and_is_600_and_reuses_key(tmp_path):
    """This machine only until local network access is turned on: a user unit cannot
    fence a wildcard bind, so the wildcard is never the default."""
    (tmp_path / runtime.CREDENTIALS_NAME).write_text(
        "username=vnc\npassword=hunter2x\n", encoding="utf-8"
    )
    calls = []
    config = runtime.provision(tmp_path, generate_key=lambda p: calls.append(p) or fake_key(p))
    text = config.read_text(encoding="utf-8")
    assert "address=127.0.0.1" in text
    assert "enable_auth=true" in text
    assert runtime.lan_access(tmp_path) is False
    runtime.provision(tmp_path, address=runtime.LAN_ADDRESS, generate_key=fake_key)
    assert runtime.lan_access(tmp_path) is True
    assert "relax_encryption=false" in text
    assert "username=vnc" in text
    assert "password=hunter2x" in text
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / runtime.KEY_NAME).stat().st_mode) == 0o600
    # A second provision must not regenerate the key.
    runtime.provision(tmp_path, generate_key=lambda p: calls.append(p))
    assert len(calls) == 1


def test_provision_rejects_privileged_port(tmp_path):
    with pytest.raises(ValueError, match="unprivileged"):
        runtime.provision(tmp_path, port=443, generate_key=fake_key)


def test_serve_execs_wayvnc_when_ready(tmp_path):
    runtime.set_password(tmp_path, read_secret=lambda _p: "hunter2x")
    execed = {}
    runtime.serve(
        tmp_path,
        capabilities=WLR,
        env={"XDG_SESSION_TYPE": "wayland"},
        host=runtime.Host(
            lambda name: "/usr/bin/wayvnc" if name == "wayvnc" else None,
            lambda path, argv: execed.update(path=path, argv=argv),
        ),
        generate_key=fake_key,
    )
    assert execed["path"] == "/usr/bin/wayvnc"
    assert execed["argv"][0] == "/usr/bin/wayvnc"
    assert "--config" in execed["argv"]


def test_serve_refuses_x11_missing_password_and_missing_binary(tmp_path):
    with pytest.raises(RuntimeError, match="native Wayland"):
        runtime.serve(
            tmp_path,
            capabilities=WLR,
            env={"XDG_SESSION_TYPE": "x11"},
            host=runtime.Host(lambda _n: "/usr/bin/wayvnc", lambda *_a: None),
        )
    # No credential is not a refusal any more: serve provisions a random one so the
    # service runs right after install -- but it still never serves unauthenticated.
    with pytest.raises(RuntimeError, match="wayvnc server binary"):
        runtime.serve(
            tmp_path,
            capabilities=WLR,
            env={"XDG_SESSION_TYPE": "wayland"},
            host=runtime.Host(lambda _n: None, lambda *_a: None),
            generate_key=fake_key,
        )


def test_serve_refuses_group_readable_config(tmp_path, monkeypatch):
    runtime.set_password(tmp_path, read_secret=lambda _p: "hunter2x")

    def loosen_key(path):
        fake_key(path)

    # Pre-create a config that provision will rewrite to 600; then loosen after provision
    # by making generate_key also chmod the dir is not needed — instead patch provision.
    original = runtime.provision

    def loose_provision(directory, **kwargs):
        path = original(directory, **kwargs)
        path.chmod(0o644)
        return path

    monkeypatch.setattr(runtime, "provision", loose_provision)
    host = runtime.Host(lambda _n: "/usr/bin/wayvnc", lambda *_a: None)
    with pytest.raises(RuntimeError, match="group/world accessible"):
        runtime.serve(
            tmp_path,
            capabilities=WLR,
            env={"XDG_SESSION_TYPE": "wayland"},
            host=host,
            generate_key=loosen_key,
        )


def test_serve_generates_a_credential_on_first_run(tmp_path, capsys):
    """Install must leave the service running, so a missing password is generated."""
    execed = {}
    runtime.serve(
        tmp_path,
        capabilities=WLR,
        env={"XDG_SESSION_TYPE": "wayland"},
        host=runtime.Host(
            lambda _n: "/usr/bin/wayvnc",
            lambda path, argv: execed.update(path=path, argv=argv),
        ),
        generate_key=fake_key,
    )
    stored = runtime.read_credentials(tmp_path)
    assert stored.username == "vnc"
    assert 6 <= len(stored.password) <= 64
    path = tmp_path / runtime.CREDENTIALS_NAME
    assert format(path.stat().st_mode & 0o777, "03o") == "600"
    assert "random one was generated" in capsys.readouterr().err
    assert execed["path"] == "/usr/bin/wayvnc"
    # A second run keeps the same secret rather than rotating it.
    again, generated = runtime.ensure_credentials(tmp_path)
    assert again == stored
    assert generated is False


GRD_PID = 4242  # what `systemctl show --property=MainPID` answers for the fake daemon


def _grd_host(calls, *, vnc_enabled):
    """A GNOME host: grdctl/gsettings/systemctl present, every call recorded."""

    def run(args, stdin=None):
        calls.append((args[1:], stdin))
        if args[0].endswith("gsettings"):
            return __import__("subprocess").CompletedProcess(
                args, 0, "true" if vnc_enabled else "false", ""
            )
        if "--property=MainPID" in args:
            return __import__("subprocess").CompletedProcess(args, 0, f"{GRD_PID}\n", "")
        return __import__("subprocess").CompletedProcess(args, 0, "", "")

    execed = {}
    host = runtime.Host(
        lambda n: f"/usr/bin/{n}" if n in ("grdctl", "gsettings", "systemctl") else None,
        lambda path, argv: execed.update(path=path, argv=argv),
        run,
        # The keyring is injected, never the developer's: unknown here, so the
        # credentials file is left as written.
        grd_password=lambda: None,
    )
    return host, execed


def _serve_gnome(tmp_path, host, listener=lambda _port: GRD_PID):
    host = dataclasses.replace(host, listener=listener)
    runtime.serve(tmp_path, env={"XDG_SESSION_TYPE": "wayland"}, host=host, capabilities=GNOME)


def test_serve_on_gnome_adopts_an_enabled_grd_and_never_touches_its_password(tmp_path, capsys):
    calls = []
    host, execed = _grd_host(calls, vnc_enabled=True)
    _serve_gnome(tmp_path, host)
    # Adopted: no enable, no set-password -- the user's existing VNC password stands.
    assert not any("set-password" in a for a, _ in calls)
    assert not any(a[:2] == ["vnc", "enable"] for a, _ in calls)
    assert (["--user", "start", runtime.GRD_UNIT], None) in calls
    # Configuration only: gnome-remote-desktop owns the socket and its own lifetime,
    # so nothing is exec'd. Mirroring its lifetime with `start --wait` used to make
    # this unit fail and restart in a loop.
    assert execed == {}
    assert not any("--wait" in a for a, _ in calls), "must not block on the grd unit"
    assert "already enabled" in capsys.readouterr().err


def test_serve_on_gnome_adopts_the_daemons_password_into_our_file(tmp_path, capsys):
    """The keyring is what the daemon checks and what the window shows; it can change
    behind our back. When it differs, the file follows it, never the other way round."""
    calls = []
    host, _execed = _grd_host(calls, vnc_enabled=True)
    runtime.set_password(tmp_path, read_secret=lambda _p: "ourpass1", username="vnc")
    host = dataclasses.replace(host, grd_password=lambda: "grdpass2")
    _serve_gnome(tmp_path, host)
    assert runtime.read_credentials(tmp_path).password == "grdpass2"
    assert "now matches the daemon" in capsys.readouterr().err
    assert not any("set-password" in a for a, _ in calls), "the daemon's own value is never touched"
    # A keyring value our own rules reject (too short) is shown but not copied.
    host = dataclasses.replace(host, grd_password=lambda: "abc")
    _serve_gnome(tmp_path, host)
    assert runtime.read_credentials(tmp_path).password == "grdpass2"


def test_serve_on_gnome_enables_a_disabled_grd_and_seeds_our_password(tmp_path):
    calls = []
    host, execed = _grd_host(calls, vnc_enabled=False)
    _serve_gnome(tmp_path, host)
    stored = runtime.read_credentials(tmp_path)
    assert (["vnc", "enable"], None) in calls
    assert (["vnc", "set-auth-method", "password"], None) in calls
    # Seeded over stdin, never argv.
    assert (["vnc", "set-password"], stored.password + "\n") in calls
    assert (["--user", "start", runtime.GRD_UNIT], None) in calls
    assert execed == {}, "the grd path configures and returns; it does not exec"


def test_serve_on_gnome_points_the_daemon_at_loopback_by_default(tmp_path):
    """The private daemon binds where the stored bind says; with nothing stored that
    is loopback, written as a drop-in before the daemon is started."""
    calls = []
    host, _execed = _grd_host(calls, vnc_enabled=True)
    _serve_gnome(tmp_path, host)
    dropin = runtime.grd_listen_dropin_path()
    assert "Environment=WAYLAND_VNC_LISTEN_ADDRESS=127.0.0.1" in dropin.read_text()
    reload = calls.index((["--user", "daemon-reload"], None))
    start = calls.index((["--user", "start", runtime.GRD_UNIT], None))
    assert reload < start, "the drop-in is loaded before the daemon starts"


def test_serve_on_gnome_restarts_a_running_daemon_when_the_stored_bind_changed(tmp_path):
    """Local network access turned on while GNOME's daemon was already up: a plain
    start would leave it on loopback, so a changed address means a restart."""
    calls = []
    host, _execed = _grd_host(calls, vnc_enabled=True)
    _serve_gnome(tmp_path, host)  # writes the loopback drop-in
    runtime.provision(tmp_path, address=runtime.LAN_ADDRESS, generate_key=fake_key)
    calls.clear()

    def active_run(args, stdin=None):
        if args[-2:] == ["is-active", runtime.GRD_UNIT]:
            return subprocess.CompletedProcess(args, 0, "active\n", "")
        return host.run(args, stdin)

    _serve_gnome(tmp_path, dataclasses.replace(host, run=active_run))
    assert (["--user", "restart", runtime.GRD_UNIT], None) in calls
    assert (["--user", "start", runtime.GRD_UNIT], None) not in calls
    # The wildcard is an EMPTY value: the daemon then binds every interface as
    # upstream does (IPv4 and IPv6), not the IPv4-only listener "0.0.0.0" would give.
    dropin = runtime.grd_listen_dropin_path().read_text()
    assert "Environment=WAYLAND_VNC_LISTEN_ADDRESS=\n" in dropin


def test_serve_on_gnome_restarts_a_daemon_that_is_active_but_not_listening(
    tmp_path, capsys, monkeypatch
):
    """Seen at login: two user sessions overlap, the new daemon's bind on 5900 fails
    and gnome-remote-desktop never retries, so the session has no VNC server while
    its unit says active. The serve path must notice and restart it once."""
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: None)
    calls = []
    host, _execed = _grd_host(calls, vnc_enabled=True)
    probes = iter([None] * 41 + [GRD_PID] * 5)  # 40 waits, one "who holds it" look, then ours
    _serve_gnome(tmp_path, host, listener=lambda _port: next(probes))
    assert (["--user", "restart", runtime.GRD_UNIT], None) in calls
    assert "not listening" in capsys.readouterr().err


def test_serve_on_gnome_waits_out_another_sessions_daemon_on_the_port(
    tmp_path, capsys, monkeypatch
):
    """The login race on this laptop: a second, short-lived session of the same user
    runs its own gnome-remote-desktop, which holds 5900 while ours fails to bind.
    A listener that is not our daemon's pid is an impostor: wait for it to go,
    restart ours, and only then accept the port."""
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: None)
    calls = []
    host, _execed = _grd_host(calls, vnc_enabled=True)
    impostor = 999
    probes = iter([impostor] * 45 + [None] * 3 + [GRD_PID] * 5)
    _serve_gnome(tmp_path, host, listener=lambda _port: next(probes))
    assert calls.count((["--user", "restart", runtime.GRD_UNIT], None)) == 1
    assert "held by another process" in capsys.readouterr().err


def test_serve_on_gnome_fails_when_the_port_stays_held_by_someone_else(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: None)
    calls = []
    host, _execed = _grd_host(calls, vnc_enabled=True)
    with pytest.raises(RuntimeError, match="held by process 999"):
        _serve_gnome(tmp_path, host, listener=lambda _port: 999)
    assert not any(a[:2] == ["--user", "restart"] for a, _ in calls), "no pointless restart"


def test_serve_on_gnome_fails_loudly_when_no_listener_appears_after_the_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: None)
    calls = []
    host, _execed = _grd_host(calls, vnc_enabled=True)
    with pytest.raises(RuntimeError, match="did not open port 5900"):
        _serve_gnome(tmp_path, host, listener=lambda _port: None)
    assert calls.count((["--user", "restart", runtime.GRD_UNIT], None)) == 1, "restart once"


def test_listener_pid_reads_the_kernel_tables_and_never_connects(tmp_path):
    # /proc/net/tcp columns: sl local rem st tx:rx tr:when retrnsmt uid timeout inode
    def row(sl, local, state, inode):
        return (
            f"{sl}: {local} 00000000:0000 {state} 00000000:00000000 00:00000000 0 1000 0 {inode}\n"
        )

    table = tmp_path / "tcp"
    table.write_text(
        "  sl  local_address rem_address   st ...\n"
        + row(0, "00000000:170C", "0A", 777001)  # 5900 listening
        + row(1, "0100007F:0016", "01", 777002)  # 22 established, not listening
    )
    proc = tmp_path / "proc"
    (proc / "31337" / "fd").mkdir(parents=True)
    (proc / "31337" / "fd" / "7").symlink_to("socket:[777001]")
    (proc / "notapid").mkdir()
    assert runtime.listener_pid(5900, (table, tmp_path / "missing"), proc) == 31337
    assert runtime.listener_pid(22, (table,), proc) is None, "established is not listening"
    assert runtime.listener_pid(5900, (tmp_path / "missing",), proc) is None
    (proc / "31337" / "fd" / "7").unlink()
    assert runtime.listener_pid(5900, (table,), proc) == 0, "listening, but not one of ours"


def test_serve_on_gnome_without_grd_tools_fails_with_a_clear_reason(tmp_path):
    host = runtime.Host(lambda _n: None, lambda *_a: None)
    with pytest.raises(RuntimeError, match="GNOME Remote Desktop"):
        runtime.serve(tmp_path, env={"XDG_SESSION_TYPE": "wayland"}, host=host, capabilities=GNOME)


def _tools(name):
    return f"/usr/bin/{name}" if name in ("grdctl", "systemctl") else None


def test_sync_backend_password_pushes_into_grd_over_stdin_and_applies_it(monkeypatch):
    """Writing the keyring is not enough: the daemon caches the old password."""
    monkeypatch.setattr(runtime, "grd_password", lambda **_kw: "previous1")
    calls = []

    def run(args, stdin=None):
        calls.append((args, stdin))
        return subprocess.CompletedProcess(args, 0, "", "")

    creds = runtime.Credentials("vnc", "hunter2x")
    assert runtime.sync_backend_password(creds, which=_tools, run=run, capabilities=GNOME) == "grd"
    # The secret goes over stdin, never argv.
    assert (["/usr/bin/grdctl", "vnc", "set-password"], "hunter2x\n") in calls
    assert all("hunter2x" not in " ".join(args) for args, _ in calls)
    # ...and the daemon is restarted so the new password actually takes effect.
    assert (["/usr/bin/systemctl", "--user", "restart", runtime.GRD_UNIT], None) in calls
    assert calls.index((["/usr/bin/grdctl", "vnc", "set-password"], "hunter2x\n")) < calls.index(
        (["/usr/bin/systemctl", "--user", "restart", runtime.GRD_UNIT], None)
    ), "the password must be stored before the daemon reloads it"


def test_sync_backend_password_reports_a_failed_restart_instead_of_claiming_success(monkeypatch):
    """Stored but not applied is the exact silent failure this must never repeat."""
    monkeypatch.setattr(runtime, "grd_password", lambda **_kw: "previous1")

    def run(args, stdin=None):
        if args[1:2] == ["--user"]:
            return subprocess.CompletedProcess(args, 1, "", "unit refused to restart")
        return subprocess.CompletedProcess(args, 0, "", "")

    creds = runtime.Credentials("vnc", "hunter2x")
    with pytest.raises(RuntimeError, match="could not be restarted"):
        runtime.sync_backend_password(creds, which=_tools, run=run, capabilities=GNOME)


def test_sync_backend_password_skips_a_backend_that_reads_our_config():
    def run(args, stdin=None):
        raise AssertionError("WayVNC reads our config directly; nothing to push")

    creds = runtime.Credentials("vnc", "hunter2x")
    assert runtime.sync_backend_password(creds, which=_tools, run=run, capabilities=WLR) is None


def test_sync_backend_password_does_not_restart_when_nothing_changed(monkeypatch):
    """Re-saving the same password must not drop live sessions for no reason."""
    calls = []

    def run(args, stdin=None):
        calls.append((args, stdin))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runtime, "grd_password", lambda **_kw: "hunter2x")
    creds = runtime.Credentials("vnc", "hunter2x")
    assert runtime.sync_backend_password(creds, which=_tools, run=run, capabilities=GNOME) == "grd"
    assert calls == [], "an unchanged password must touch neither grdctl nor systemd"


@pytest.mark.parametrize(
    "stored,expected",
    [("'quoted'", "quoted"), ("plain", "plain"), ("", ""), (None, None)],
)
def test_grd_password_unwraps_the_gvariant_quoting(monkeypatch, real_runners, stored, expected):
    """grd stores the secret GVariant-serialised, so it comes back single-quoted."""

    class FakeSecret:
        Schema = type("S", (), {"new": staticmethod(lambda *a: "schema")})
        SchemaFlags = type("F", (), {"NONE": 0})

        @staticmethod
        def password_lookup_sync(*_args):
            return stored

    monkeypatch.setattr(
        runtime.importlib,
        "import_module",
        lambda name: (
            type("Gi", (), {"require_version": staticmethod(lambda *a: None)})
            if name == "gi"
            else FakeSecret
        ),
    )
    assert real_runners.grd_password() == expected


def test_grd_password_is_unknown_when_the_keyring_is_locked(monkeypatch, real_runners):
    """The settings window asks for this on every GNOME host; a locked keyring or a
    missing Secret Service answers with a GLib.Error, and that must read as unknown."""

    class FakeGLib:
        class Error(Exception):
            pass

    class FakeSecret:
        Schema = type("S", (), {"new": staticmethod(lambda *a: "schema")})
        SchemaFlags = type("F", (), {"NONE": 0})

        @staticmethod
        def password_lookup_sync(*_args):
            raise FakeGLib.Error("The name org.freedesktop.secrets was not provided")

    fakes = {
        "gi": type("Gi", (), {"require_version": staticmethod(lambda *a: None)}),
        "gi.repository.Secret": FakeSecret,
        "gi.repository.GLib": FakeGLib,
    }
    monkeypatch.setattr(runtime.importlib, "import_module", fakes.__getitem__)
    assert real_runners.grd_password() is None


def test_grd_password_is_unknown_without_libsecret(monkeypatch, real_runners):
    def missing(_name):
        raise ImportError("no gi")

    monkeypatch.setattr(runtime.importlib, "import_module", missing)
    assert real_runners.grd_password() is None


def test_wait_for_backend_returns_as_soon_as_the_unit_is_active(monkeypatch):
    answers = iter(["activating", "activating", "active"])
    monkeypatch.setattr(runtime, "backend_state", lambda *_a, **_kw: next(answers))
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: None)
    assert runtime.wait_for_backend() is True


def test_wait_for_backend_gives_up_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(runtime, "backend_state", lambda *_a, **_kw: "activating")
    slept = []
    monkeypatch.setattr(runtime.time, "sleep", slept.append)
    assert runtime.wait_for_backend(attempts=4) is False
    assert len(slept) == 4, "must be bounded, not an unbounded spin"


def test_sync_waits_for_the_backend_before_returning(monkeypatch):
    """A phone told a new password must not race the daemon's restart."""
    calls = []

    def run(args, stdin=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    waited = []
    monkeypatch.setattr(runtime, "grd_password", lambda **_kw: "different")
    monkeypatch.setattr(runtime, "wait_for_backend", lambda **_kw: waited.append(True) or True)
    creds = runtime.Credentials("vnc", "hunter2x")
    runtime.sync_backend_password(creds, which=_tools, run=run, capabilities=GNOME)
    assert waited == [True], "must wait for the backend after restarting it"
    assert calls[-1] == ["/usr/bin/systemctl", "--user", "restart", runtime.GRD_UNIT]


def test_backend_password_limit_is_eight_on_grd_and_open_on_wayvnc():
    """Classic VncAuth is an 8-byte DES key; grd refuses anything longer outright."""
    assert runtime.backend_password_limit(GNOME) == runtime.VNC_AUTH_MAX_PASSWORD == 8
    assert runtime.backend_password_limit(WLR) is None


def test_check_password_for_backend_refuses_before_anything_is_written(tmp_path):
    with pytest.raises(ValueError, match="at most 8 characters"):
        runtime.check_password_for_backend("waytoolong", GNOME)
    # Exactly at the limit is fine, and an unconstrained backend takes any length.
    runtime.check_password_for_backend("12345678", GNOME)
    runtime.check_password_for_backend("a-much-longer-password", WLR)


def test_backend_is_serving_asks_systemd_and_never_opens_a_connection():
    """The probe must not speak VNC.

    On this GNOME build every inbound connection segfaults gnome-remote-desktop, so a
    probe that connected would crash the daemon it is waiting for and hand the next
    real client a refused connection -- the very failure the probe exists to prevent.
    """
    seen = []

    def run(args, stdin=None):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "active\n", "")

    assert runtime.backend_is_serving(run) is True
    assert seen == [["systemctl", "--user", "is-active", runtime.GRD_UNIT]]
    assert not hasattr(runtime, "socket"), "readiness must not reach for a socket at all"


@pytest.mark.parametrize("state,expected", [("active", True), ("activating", False), ("", False)])
def test_backend_is_serving_treats_anything_but_active_as_not_ready(state, expected):
    def run(args, stdin=None):
        return subprocess.CompletedProcess(args, 0, state + "\n", "")

    assert runtime.backend_is_serving(run) is expected


def test_wait_for_backend_settles_after_systemd_reports_active(monkeypatch):
    """systemd calls a Type=dbus unit started once its bus name appears, which is a
    little before the VNC listener accepts; the settle pause covers that window."""
    monkeypatch.setattr(runtime, "backend_state", lambda *_a, **_kw: "active")
    slept = []
    monkeypatch.setattr(runtime.time, "sleep", slept.append)
    assert runtime.wait_for_backend(settle=1.5) is True
    assert slept == [1.5], "must pause for the settle window, and only that"


def _states(*sequence):
    """A runner that reports the given unit states in turn and records every command."""
    calls = []
    answers = iter(sequence)

    def run(args, stdin=None):
        calls.append(args)
        if args[-2:] == ["is-active", runtime.GRD_UNIT]:
            return subprocess.CompletedProcess(args, 0, next(answers) + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    return run, calls


def test_wait_for_backend_revives_a_unit_systemd_gave_up_on(monkeypatch):
    """grd segfaults when a session ends; enough crashes trip its start limit and
    systemd then refuses to start it at all. Leaving it dead would strand the desktop
    with no VNC server, so the limit is cleared and the unit started again."""
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: None)
    run, calls = _states("failed", "activating", "active")
    assert runtime.wait_for_backend(run=run) is True
    assert ["systemctl", "--user", "reset-failed", runtime.GRD_UNIT] in calls
    assert ["systemctl", "--user", "start", runtime.GRD_UNIT] in calls


def test_wait_for_backend_revives_only_once_and_never_loops_on_it(monkeypatch):
    """A unit that stays failed must not be hammered with restarts."""
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: None)
    run, calls = _states(*["failed"] * 6)
    assert runtime.wait_for_backend(run=run, attempts=6) is False
    assert calls.count(["systemctl", "--user", "start", runtime.GRD_UNIT]) == 1


def test_ensure_grd_restart_tolerance_widens_the_start_limit(tmp_path):
    """A disconnect crashes grd; without a wider limit a few reconnects leave systemd
    refusing to start it at all, and the desktop with no VNC server."""
    calls = []

    def run(args, stdin=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    assert runtime.ensure_grd_restart_tolerance(run=run, which=_tools, env=env) is True
    written = runtime.grd_dropin_path(env)
    assert written.parent.name == f"{runtime.GRD_UNIT}.d"
    assert "StartLimitBurst=20" in written.read_text()
    assert ["/usr/bin/systemctl", "--user", "daemon-reload"] in calls


def test_apply_bind_restarts_wayvnc_and_the_private_grd_but_not_the_distribution_daemon(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: None)
    calls = []

    def run(args, stdin=None):
        calls.append(args)
        state = "active\n" if args[-2:-1] == ["is-active"] else ""
        return subprocess.CompletedProcess(args, 0, state, "")

    runtime.provision(tmp_path, generate_key=fake_key)
    assert runtime.apply_bind(tmp_path, which=_tools, run=run, capabilities=WLR) == "wayvnc"
    assert ["/usr/bin/systemctl", "--user", "restart", runtime.UNIT] in calls
    calls.clear()
    # GNOME with the distribution's daemon: nothing can be applied, and nothing is touched.
    assert (
        runtime.apply_bind(
            tmp_path, which=_tools, run=run, capabilities=GNOME, private_grd=lambda: False
        )
        is None
    )
    assert calls == []
    assert not runtime.grd_listen_dropin_path().exists()
    # GNOME with the private daemon: the drop-in is written and the daemon restarted.
    assert (
        runtime.apply_bind(
            tmp_path, which=_tools, run=run, capabilities=GNOME, private_grd=lambda: True
        )
        == "grd"
    )
    assert ["/usr/bin/systemctl", "--user", "restart", runtime.GRD_UNIT] in calls
    calls.clear()
    # Unchanged address: no rewrite, no restart, no reload.
    runtime.apply_bind(
        tmp_path, which=_tools, run=run, capabilities=GNOME, private_grd=lambda: True
    )
    assert not any(a[-2:-1] == ["restart"] or a[-1] == "daemon-reload" for a in calls)


def test_ensure_grd_restart_tolerance_is_idempotent(tmp_path):
    """Re-running must not rewrite the file or reload systemd for no reason."""
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    runs = []

    def run(args, stdin=None):
        runs.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    runtime.ensure_grd_restart_tolerance(run=run, which=_tools, env=env)
    runs.clear()
    assert runtime.ensure_grd_restart_tolerance(run=run, which=_tools, env=env) is False
    assert runs == [], "an unchanged override must not trigger a daemon-reload"


NO_BACKEND = {**WLR, "interfaces": []}
PLASMA = {
    **WLR,
    "interfaces": [],
    "kwin": True,
    "remote_desktop_portal": True,
    "screencast_portal": True,
}


@pytest.mark.parametrize(
    "env,allowed",
    [
        ({"XDG_SESSION_TYPE": "wayland"}, True),
        ({"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}, True),
        # Unset inside a systemd user unit, but the unit is gated on a Wayland display.
        ({"WAYLAND_DISPLAY": "wayland-0"}, True),
        ({"XDG_SESSION_TYPE": "x11"}, False),
        ({"XDG_SESSION_TYPE": "tty"}, False),
        # Nothing at all says nothing about the session, so it must be refused.
        ({}, False),
    ],
)
def test_require_wayland_session_fails_closed_on_an_unknown_session(env, allowed):
    if allowed:
        runtime.require_wayland_session(env)
        return
    with pytest.raises(RuntimeError, match="native Wayland"):
        runtime.require_wayland_session(env)


def test_serve_refuses_when_the_probe_found_no_backend(tmp_path):
    """Falling through to WayVNC here would start a server that cannot capture."""
    host = runtime.Host(lambda _n: "/usr/bin/wayvnc", lambda *_a: None)
    with pytest.raises(RuntimeError, match="Refusing to serve"):
        runtime.serve(
            tmp_path,
            capabilities=NO_BACKEND,
            env={"XDG_SESSION_TYPE": "wayland"},
            host=host,
            generate_key=fake_key,
        )


def test_serve_refuses_a_backend_this_package_does_not_install(tmp_path):
    """Plasma is served by w0vncserver; launching WayVNC there is the wrong server."""
    backend, _reason = runtime.select_backend(PLASMA)
    assert backend == "w0vncserver", "fixture must actually select the KDE backend"
    host = runtime.Host(lambda _n: "/usr/bin/wayvnc", lambda *_a: None)
    with pytest.raises(RuntimeError, match="w0vncserver"):
        runtime.serve(
            tmp_path,
            capabilities=PLASMA,
            env={"XDG_SESSION_TYPE": "wayland"},
            host=host,
            generate_key=fake_key,
        )


def test_serve_keeps_a_configured_loopback_bind_instead_of_resetting_it(tmp_path):
    """A restart must not undo a deliberate choice to keep the server off the LAN."""
    runtime.set_password(tmp_path, read_secret=lambda _p: "hunter2x")
    runtime.provision(tmp_path, address="127.0.0.1", port=5999, generate_key=fake_key)
    runtime.serve(
        tmp_path,
        capabilities=WLR,
        env={"XDG_SESSION_TYPE": "wayland"},
        host=runtime.Host(lambda _n: "/usr/bin/wayvnc", lambda *_a: None),
        generate_key=fake_key,
    )
    assert runtime.read_config(tmp_path) == ("127.0.0.1", 5999)


def test_read_config_reports_nothing_useful_rather_than_guessing(tmp_path):
    assert runtime.read_config(tmp_path) is None
    (tmp_path / runtime.CONFIG_NAME).write_text("address=\nport=notaport\n", encoding="utf-8")
    assert runtime.read_config(tmp_path) is None


def test_refresh_config_puts_the_new_password_in_the_file_wayvnc_reads(tmp_path):
    """WayVNC reads the password from the config, so storing one is not enough."""
    runtime.set_password(tmp_path, read_secret=lambda _p: "hunter2x")
    runtime.provision(tmp_path, address="127.0.0.1", port=5999, generate_key=fake_key)
    runtime.set_password(tmp_path, read_secret=lambda _p: "changed9")
    assert "password=hunter2x" in (tmp_path / runtime.CONFIG_NAME).read_text()

    assert runtime.refresh_config(tmp_path, generate_key=fake_key) is not None
    body = (tmp_path / runtime.CONFIG_NAME).read_text()
    assert "password=changed9" in body
    assert "address=127.0.0.1" in body and "port=5999" in body, "the bind must survive"


def test_refresh_config_does_nothing_before_anything_is_provisioned(tmp_path):
    assert runtime.refresh_config(tmp_path, generate_key=fake_key) is None


@pytest.mark.parametrize("state,restarted", [("active", True), ("inactive", False)])
def test_restart_service_only_restarts_a_unit_that_is_running(state, restarted):
    """A stopped unit reads the new config when it starts; restarting it is pointless."""
    calls = []

    def run(args, stdin=None):
        calls.append(args)
        if args[-2:] == ["is-active", runtime.UNIT]:
            return subprocess.CompletedProcess(args, 0, state + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    assert runtime.restart_service(which=_tools, run=run) is restarted
    assert (["/usr/bin/systemctl", "--user", "restart", runtime.UNIT] in calls) is restarted


def test_restart_service_without_systemctl_reports_nothing_happened():
    assert runtime.restart_service(which=lambda _n: None, run=lambda *_a, **_k: None) is False
