"""The settings layer must report real state and refuse to offer unbacked options."""

import subprocess

import pytest

from wayland_vnc import runtime, settings


def capabilities(**overrides):
    result = {
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
    result.update(overrides)
    return result


def runner(mapping, *, code=0):
    """A fake systemctl: maps the verb to stdout, recording every call."""
    calls = []

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, code, mapping.get(args[0], ""), "boom")

    run.calls = calls
    return run


def key_generator(path):
    path.write_text("fake-key", encoding="utf-8")


def fake_host(*, active=False, private_grd=False):
    """The OS boundary the actions apply binds and passwords through, recorded."""
    calls = []

    def run(args, stdin=None):
        calls.append(args)
        state = ("active" if active else "inactive") + "\n"
        return subprocess.CompletedProcess(args, 0, state if "is-active" in args else "", "")

    host = runtime.Host(
        lambda n: f"/usr/bin/{n}" if n in ("systemctl", "grdctl") else None,
        lambda *_a: None,
        run,
        private_grd=lambda: private_grd,
    )
    # Host is frozen; the recorded calls ride on the runner it was given.
    host.run.calls = calls
    return host


@pytest.fixture(name="actions")
def actions_fixture(tmp_path):
    return settings.Actions(
        tmp_path,
        run=runner({"is-enabled": "enabled", "is-active": "active"}),
        generate_key=key_generator,
        capabilities=capabilities(),
        host=fake_host(),
    )


def test_service_absent_is_reported_not_guessed():
    state = settings.service_state(runner({"is-enabled": "not-found"}))
    assert not state.installed
    assert not state.enabled
    assert not state.active
    assert state.detail == "unit is not installed"


@pytest.mark.parametrize(
    "enabled,active,expect_enabled,expect_active",
    [("enabled", "active", True, True), ("disabled", "inactive", False, False)],
)
def test_service_state_reflects_systemd(enabled, active, expect_enabled, expect_active):
    state = settings.service_state(runner({"is-enabled": enabled, "is-active": active}))
    assert state.installed
    assert state.enabled is expect_enabled
    assert state.active is expect_active
    assert state.detail == f"{enabled}, {active}"


def test_gather_on_a_bare_directory_reports_nothing_configured(tmp_path):
    status = settings.gather(
        tmp_path, capabilities=capabilities(), run=runner({"is-enabled": "not-found"})
    )
    assert not status.credential.present
    assert status.credential.mode is None
    assert not status.config.present
    assert status.config.port is None
    assert not status.config.auth_enabled
    assert status.wayland


def test_gather_reads_the_real_provisioned_config(actions, tmp_path):
    actions.set_credential("vnc", "hunter2x")
    actions.apply_network("127.0.0.1", 5900)
    status = actions.status()
    assert status.credential.present
    assert status.credential.secure_mode
    assert status.config.present
    assert status.config.loopback_only
    assert status.config.port == 5900
    assert status.config.auth_enabled
    assert status.service.installed
    assert status.service.enabled
    assert status.service.active


def test_credential_is_written_restrictively(actions, tmp_path):
    path = actions.set_credential("vnc", "hunter2x")
    assert path == tmp_path / runtime.CREDENTIALS_NAME
    assert format(path.stat().st_mode & 0o777, "03o") == "600"
    assert runtime.read_credentials(tmp_path).username == "vnc"


def test_network_option_enforces_the_same_validation_as_the_cli(actions):
    actions.set_credential("vnc", "hunter2x")
    with pytest.raises(ValueError):
        actions.apply_network("127.0.0.1", 443)


def test_a_weak_password_is_refused_before_anything_is_written(actions, tmp_path):
    with pytest.raises(ValueError):
        actions.set_credential("vnc", "short")
    assert not (tmp_path / runtime.CREDENTIALS_NAME).exists()


@pytest.mark.parametrize(
    "call,argument,verb,message",
    [
        ("set_enabled", True, "enable", "service enabled"),
        ("set_enabled", False, "disable", "service disabled"),
        ("set_active", True, "start", "service started"),
        ("set_active", False, "stop", "service stopped"),
    ],
)
def test_service_controls_drive_the_real_unit(tmp_path, call, argument, verb, message):
    run = runner({})
    actions = settings.Actions(tmp_path, run=run, generate_key=key_generator)
    assert getattr(actions, call)(argument) == message
    assert run.calls[-1] == [verb, settings.UNIT]


def test_service_control_failure_surfaces_systemd_stderr(tmp_path):
    actions = settings.Actions(tmp_path, run=runner({}, code=1), generate_key=key_generator)
    with pytest.raises(RuntimeError, match="boom"):
        actions.set_enabled(True)


def test_options_are_gated_on_real_preconditions(tmp_path):
    actions = settings.Actions(
        tmp_path,
        run=runner({"is-enabled": "not-found"}),
        generate_key=key_generator,
        capabilities=capabilities(session_type="x11"),
    )
    status = actions.status()
    options = {option.key: option for option in actions.options(status)}
    assert options["credential"].available
    assert options["diagnostic"].label == "Diagnostics"
    assert not options["network"].available
    assert options["network"].reason == "Set a viewer password first"
    # The service is controlled only by the switches now, so no row duplicates them.
    assert "service" not in options
    # No unit and no credential: the reason belongs to the Service group.
    assert "systemd user unit" in actions.service_reason(status)


def test_service_reason_explains_each_blocking_reason_in_turn(actions, tmp_path):
    # Unit present, but no credential yet.
    assert actions.service_reason(actions.status()) == "Set a viewer password first"
    actions.set_credential("vnc", "hunter2x")
    # Credential present and the session is Wayland: the switches become usable.
    assert actions.service_reason(actions.status()) == ""


def test_service_reason_reports_a_non_wayland_session(tmp_path):
    actions = settings.Actions(
        tmp_path,
        run=runner({"is-enabled": "enabled", "is-active": "inactive"}),
        generate_key=key_generator,
        capabilities=capabilities(session_type="x11"),
    )
    actions.set_credential("vnc", "hunter2x")
    assert "Wayland" in actions.service_reason(actions.status())


def test_defaults_resolve_the_xdg_config_directory(monkeypatch, tmp_path):
    monkeypatch.setenv(runtime.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    assert settings.Actions().directory == tmp_path / "cfg"


@pytest.mark.parametrize(
    "failure", [FileNotFoundError("systemctl"), subprocess.TimeoutExpired("systemctl", 15)]
)
def test_a_host_without_systemctl_reports_the_unit_absent(monkeypatch, failure, real_runners):
    # Not every supported platform runs systemd; the app must report rather than crash.
    # The genuine wrapper is under test here, with subprocess.run itself failing.
    def explode(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(settings.subprocess, "run", explode)
    state = settings.service_state(run=real_runners.systemctl)
    assert not state.installed
    assert state.detail == "unit is not installed"


@pytest.mark.parametrize(
    "address,scope",
    [
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("0.0.0.0", "any-network"),
        ("::", "any-network"),
        ("192.168.1.20", "local-network"),
        ("10.0.0.5", "local-network"),
        ("169.254.10.1", "local-network"),
        ("8.8.8.8", "public"),
        ("not-an-address", "public"),
    ],
)
def test_server_config_scope_classifies_reachability(address, scope):
    """The status view must say exactly who can reach the bind."""
    config = settings.ServerConfig(True, address, 5900, True)
    assert config.scope == scope
    assert config.loopback_only is (scope == "loopback")
    assert config.lan_access is (scope == "any-network")


def test_lan_access_switch_stores_the_wildcard_and_restarts_a_running_server(tmp_path):
    """One switch: on is every network this computer is on, off is this computer
    only, on whatever port was already chosen -- applied to the running server."""
    host = fake_host(active=True)
    actions = settings.Actions(
        tmp_path,
        run=runner({}),
        generate_key=key_generator,
        capabilities=capabilities(),
        host=host,
    )
    actions.apply_network("127.0.0.1", 5901)
    assert actions.status().config.lan_access is False
    actions.set_lan_access(True)
    config = actions.status().config
    assert (config.address, config.port, config.lan_access) == ("0.0.0.0", 5901, True)
    assert ["/usr/bin/systemctl", "--user", "restart", settings.UNIT] in host.run.calls
    actions.set_lan_access(False)
    assert actions.status().config.loopback_only
    assert actions.lan_access_option(actions.status()).available


def test_lan_access_is_a_real_switch_only_where_the_backend_can_honour_it(tmp_path):
    """Insensitive with the reason, never a switch that silently does nothing."""

    def option(caps, host):
        actions = settings.Actions(
            tmp_path, run=runner({}), generate_key=key_generator, capabilities=caps, host=host
        )
        return actions.lan_access_option(actions.status())

    gnome = capabilities(interfaces=[], gnome_remote_desktop=True, gnome_screencast=True)
    assert option(gnome, fake_host(private_grd=True)).available
    distro = option(gnome, fake_host(private_grd=False))
    assert not distro.available
    assert "wayland-vnc-grd" in distro.reason
    plasma = capabilities(
        interfaces=[], kwin=True, remote_desktop_portal=True, screencast_portal=True
    )
    kde = option(plasma, fake_host())
    assert not kde.available
    assert "this computer only" in kde.reason
    nothing = option(capabilities(interfaces=[]), fake_host())
    assert not nothing.available
    assert nothing.reason == "No supported backend"


def test_lan_access_on_gnome_goes_through_the_private_daemons_drop_in(tmp_path):
    host = fake_host(active=True, private_grd=True)
    gnome = capabilities(interfaces=[], gnome_remote_desktop=True, gnome_screencast=True)
    actions = settings.Actions(
        tmp_path, run=runner({}), generate_key=key_generator, capabilities=gnome, host=host
    )
    actions.set_lan_access(True)
    assert "WAYLAND_VNC_LISTEN_ADDRESS=\n" in runtime.grd_listen_dropin_path().read_text()
    assert ["/usr/bin/systemctl", "--user", "restart", runtime.GRD_UNIT] in host.run.calls
    assert ["/usr/bin/systemctl", "--user", "restart", settings.UNIT] not in host.run.calls


def test_set_credential_on_a_gnome_host_updates_gnome_remote_desktop(tmp_path, monkeypatch):
    """The dialog's password must reach grd, over stdin, on a GNOME desktop."""
    monkeypatch.setattr(settings.runtime, "grd_password", lambda **_kw: "previous1")
    calls = []

    def backend_run(args, stdin=None):
        calls.append((args, stdin))
        return subprocess.CompletedProcess(args, 0, "", "")

    host = runtime.Host(
        lambda n: "/usr/bin/grdctl" if n == "grdctl" else None, lambda *_a: None, backend_run
    )
    gnome = capabilities(interfaces=[], gnome_remote_desktop=True, gnome_screencast=True)
    actions = settings.Actions(
        tmp_path, run=runner({}), generate_key=key_generator, capabilities=gnome, host=host
    )
    actions.set_credential("vnc", "hunter2x")
    assert calls == [(["/usr/bin/grdctl", "vnc", "set-password"], "hunter2x\n")]


IP_JSON = """[
  {"ifname": "lo",
   "addr_info": [{"family": "inet", "local": "127.0.0.1", "scope": "host"}]},
  {"ifname": "wlo1",
   "addr_info": [{"family": "inet", "local": "192.168.1.33", "scope": "global"},
                 {"family": "inet6", "local": "fe80::1", "scope": "link"}]},
  {"ifname": "docker0",
   "addr_info": [{"family": "inet", "local": "172.17.0.1", "scope": "global"}]},
  {"ifname": "lxcbr0",
   "addr_info": [{"family": "inet", "local": "10.0.3.1", "scope": "global"}]},
  {"ifname": "enp3s0",
   "addr_info": [{"family": "inet", "local": "10.1.2.3", "scope": "global"}]}
]"""


def _ip(stdout, code=0):
    def run(args):
        assert args[:2] == ["ip", "-json"], "must query iproute2, never systemctl"
        return subprocess.CompletedProcess(args, code, stdout, "")

    return run


def test_local_addresses_show_real_interfaces_and_hide_virtual_bridges():
    # A phone cannot reach docker0 or lxcbr0; loopback and IPv6 link-local are not
    # what a viewer types either.
    assert settings.local_addresses(_ip(IP_JSON)) == (
        ("wlo1", "192.168.1.33"),
        ("enp3s0", "10.1.2.3"),
    )


@pytest.mark.parametrize("stdout,code", [("", 1), ("not json", 0), ("[]", 0)])
def test_local_addresses_report_nothing_rather_than_guess(stdout, code):
    assert settings.local_addresses(_ip(stdout, code)) == ()


def test_connect_info_lists_the_mdns_name_first_with_the_real_port():
    config = settings.ServerConfig(True, "0.0.0.0", 5901, True)
    info = settings.connect_info(config, run=_ip(IP_JSON), hostname="zenbook")
    assert info.port == 5901
    assert info.targets[0] == (settings._("mDNS name"), "zenbook.local:5901")
    assert ("wlo1", "192.168.1.33:5901") in info.targets


def test_connect_info_falls_back_to_the_default_port_when_unprovisioned():
    config = settings.ServerConfig(False, None, None, False)
    info = settings.connect_info(config, run=_ip("[]"), hostname="zenbook")
    assert info.port == runtime.DEFAULT_PORT
    assert info.addresses == ()


def test_diagnostic_sections_group_the_flat_report_into_readable_categories():
    """Every row must come from the report; nothing invented, nothing dropped."""
    report = {
        "backend_candidate": "wayvnc",
        "reason": "capture and input are present",
        "qualification": "unqualified",
        "ready_to_install": False,
        "release_targets": ["sway", "hyprland"],
        "capabilities": capabilities(
            interfaces=["zwlr_screencopy_manager_v1"],
            binaries={"wayvnc": "/usr/bin/wayvnc", "w0vncserver": None},
        ),
    }
    sections = {s.key: s for s in settings.diagnostic_sections(report)}
    assert sections["verdict"].summary == "wayvnc"
    assert ("Backend Candidate", "wayvnc") in sections["verdict"].rows
    assert sections["session"].summary == "wayland"
    assert sections["protocols"].rows == (("zwlr_screencopy_manager_v1", "Advertised"),)
    # The summary must say whether anything that MATTERS is missing. w0vncserver is
    # KDE Plasma's server; on a wayvnc host its absence is normal, not a fault.
    assert sections["binaries"].summary == (
        "Everything this desktop needs; not installed: w0vncserver"
    )
    rows = dict(sections["binaries"].rows)
    assert rows["w0vncserver"] == "Not installed — TigerVNC server, for KDE Plasma"
    assert rows["wayvnc"] == "/usr/bin/wayvnc — WayVNC server, for wlroots compositors"
    assert sections["targets"].summary == "2 desktops"


def test_diagnostic_sections_describe_an_unsupported_host_without_crashing():
    sections = {s.key: s for s in settings.diagnostic_sections({"capabilities": {}})}
    assert sections["verdict"].summary == "No supported backend"
    assert sections["protocols"].summary == "None advertised"
    assert sections["protocols"].rows == (("None", "Not advertised"),)
    assert sections["portals"].summary == "None available"
    assert sections["services"].summary == "None detected"
    assert sections["binaries"].summary == "None checked"


@pytest.mark.parametrize(
    "binaries,backend,expected",
    [
        ({"wayvnc": "/usr/bin/wayvnc"}, "wayvnc", "All present"),
        (
            {"wayvnc": None, "gdbus": "/usr/bin/gdbus"},
            "wayvnc",
            "wayvnc is MISSING and this desktop needs it",
        ),
        (
            {"wayvnc": "/usr/bin/wayvnc", "w0vncserver": None},
            "wayvnc",
            "Everything this desktop needs; not installed: w0vncserver",
        ),
        ({}, "wayvnc", "None checked"),
    ],
)
def test_binary_summary_reports_what_matters_not_a_bare_count(binaries, backend, expected):
    assert settings._binary_summary(binaries, backend) == expected


def test_binary_summary_does_not_claim_an_unprobed_binary_is_missing():
    """GNOME Remote Desktop is detected over D-Bus; its daemon is never probed by name,
    so a lookup miss must not be reported as a missing dependency."""
    binaries = {"wayvnc": "/usr/bin/wayvnc", "w0vncserver": None}
    summary = settings._binary_summary(binaries, "grd")
    assert "gnome-remote-desktop-daemon" not in summary
    assert summary == "Everything this desktop needs; not installed: w0vncserver"


def test_credential_returns_the_stored_pair(tmp_path):
    # Pin the capabilities: without them this probes the developer's own desktop, and
    # on a GNOME host `credential()` would consult grd's keyring instead of our file.
    actions = settings.Actions(
        tmp_path, run=runner({}), generate_key=key_generator, capabilities=capabilities()
    )
    assert actions.credential() is None
    actions.set_credential("vnc", "phone123")
    stored = actions.credential()
    assert stored.username == "vnc"
    assert stored.password == "phone123"


def test_credential_survives_an_unreadable_file(tmp_path):
    """A corrupt credential must not break the dialog; the user can just set a new one."""
    actions = settings.Actions(
        tmp_path, run=runner({}), generate_key=key_generator, capabilities=capabilities()
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / runtime.CREDENTIALS_NAME).write_text("garbage\n", encoding="utf-8")
    assert actions.credential() is None


def test_credential_prefers_what_the_grd_backend_actually_enforces(tmp_path, monkeypatch):
    """serve adopts an already-enabled grd, so our file and grd's keyring can differ.

    Showing our copy hands the user a password the server rejects -- the exact failure
    reported from use ("I entered the correct password").
    """
    gnome = capabilities(interfaces=[], gnome_remote_desktop=True, gnome_screencast=True)
    host = fake_host()
    actions = settings.Actions(
        tmp_path, run=runner({}), generate_key=key_generator, capabilities=gnome, host=host
    )
    # What grd's keyring holds is injected, never read: the two copies differ here.
    monkeypatch.setattr(settings.runtime, "grd_password", lambda **_kw: "grdvalue")
    actions.set_credential("vnc", "ourcopy1")
    assert ["/usr/bin/grdctl", "vnc", "set-password"] in host.run.calls
    shown = actions.credential()
    assert shown.password == "grdvalue", "must show what the server accepts"
    assert shown.username == "vnc"


def test_credential_falls_back_to_our_file_when_grd_has_none(tmp_path, monkeypatch):
    gnome = capabilities(interfaces=[], gnome_remote_desktop=True, gnome_screencast=True)
    actions = settings.Actions(
        tmp_path, run=runner({}), generate_key=key_generator, capabilities=gnome, host=fake_host()
    )
    monkeypatch.setattr(settings.runtime, "grd_password", lambda **_kw: None)
    actions.set_credential("vnc", "ourcopy1")
    assert actions.credential().password == "ourcopy1"


def test_credential_shows_a_grd_password_our_own_rules_would_reject(tmp_path, monkeypatch):
    """grd accepts passwords outside our 6-64 rule; hiding them would be worse."""
    gnome = capabilities(interfaces=[], gnome_remote_desktop=True, gnome_screencast=True)
    actions = settings.Actions(
        tmp_path, run=runner({}), generate_key=key_generator, capabilities=gnome
    )
    monkeypatch.setattr(settings.runtime, "grd_password", lambda: "abc")
    shown = actions.credential()
    assert shown.password == "abc"
    assert shown.username == "vnc"


def test_credential_on_a_wayvnc_host_uses_our_own_file(tmp_path, monkeypatch):
    """WayVNC reads our config, so our file is the authority there."""
    actions = settings.Actions(
        tmp_path, run=runner({}), generate_key=key_generator, capabilities=capabilities()
    )
    actions.set_credential("vnc", "ourcopy1")
    monkeypatch.setattr(
        settings.runtime, "grd_password", lambda: (_ for _ in ()).throw(AssertionError("no grd"))
    )
    assert actions.credential().password == "ourcopy1"


def test_set_credential_refuses_a_password_the_grd_backend_cannot_accept(tmp_path):
    """Writing first and failing the sync afterwards left our file holding a password
    the server would never accept; refuse up front instead."""
    gnome = capabilities(interfaces=[], gnome_remote_desktop=True, gnome_screencast=True)
    actions = settings.Actions(
        tmp_path, run=runner({}), generate_key=key_generator, capabilities=gnome
    )
    with pytest.raises(ValueError, match="at most 8 characters"):
        actions.set_credential("vnc", "waytoolongpassword")
    assert not (tmp_path / runtime.CREDENTIALS_NAME).exists(), "nothing may be written"
