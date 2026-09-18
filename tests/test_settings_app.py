"""The settings window must render real state and disable unbacked controls.

GTK needs a display to construct widgets, so these tests run against a private Xvfb
server rather than the developer's session; nothing is ever presented on screen.
"""

import re
import subprocess
from pathlib import Path

import pytest

from wayland_vnc import i18n, runtime, settings, settings_app
from wayland_vnc.settings import CredentialState, ServerConfig, ServiceState, Status


@pytest.fixture(autouse=True)
def _automatic_language_afterwards():
    """The active language is process-wide state. Whatever a test activates, or
    asserts on part-way and fails, the next test starts from Automatic."""
    yield
    i18n.activate(i18n.AUTOMATIC)


def _status(**overrides):
    base = {
        "backend_candidate": "wayvnc",
        "reason": "A compatible capture and input protocol set is present.",
        "session_type": "wayland",
        "credential": CredentialState(True, "600"),
        "config": ServerConfig(True, "127.0.0.1", 5900, True),
        "service": ServiceState(True, True, True, "enabled, active"),
        "diagnostic": {"qualification": "unqualified"},
    }
    base.update(overrides)
    return Status(**base)


def test_status_rows_report_a_provisioned_loopback_server():
    rows = dict(settings_app.status_rows(_status()))
    assert rows["Backend"] == "wayvnc"
    assert rows["Viewer Credential"] == "Stored (mode 600)"
    assert "this machine only" in rows["Server Configuration"]
    assert "auth Yes" in rows["Server Configuration"]


def test_status_rows_describe_a_lan_bind_and_a_loose_credential():
    rows = dict(
        settings_app.status_rows(
            _status(
                credential=CredentialState(True, "644"),
                config=ServerConfig(True, "0.0.0.0", 5900, False),
            )
        )
    )
    assert "too open" in rows["Viewer Credential"]
    # A wildcard bind is the local-network opt-in; nothing fences a user service, so
    # the row says what that really reaches.
    assert "every network this computer is on" in rows["Server Configuration"]
    assert "auth No" in rows["Server Configuration"]


def test_status_rows_describe_the_gnome_daemons_bind_only_when_it_is_the_private_one():
    """With the private daemon the stored bind is where it listens (always 5900, always
    a password); with the distribution's daemon our config says nothing about it."""
    gnome = _status(backend_candidate="grd", config=ServerConfig(True, "0.0.0.0", 5901, True))
    private = dict(settings_app.status_rows(gnome, private_grd=True))["Server Configuration"]
    assert private.startswith("0.0.0.0:5900 (every network this computer is on)")
    distribution = dict(settings_app.status_rows(gnome))["Server Configuration"]
    assert "GNOME Settings" in distribution
    unprovisioned = _status(backend_candidate="grd", config=ServerConfig(False, None, None, False))
    assert (
        "127.0.0.1:5900 (this machine only)"
        in dict(settings_app.status_rows(unprovisioned, private_grd=True))["Server Configuration"]
    )


def test_status_rows_shout_about_a_public_address():
    rows = dict(settings_app.status_rows(_status(config=ServerConfig(True, "8.8.8.8", 5900, True))))
    assert "PUBLIC ADDRESS" in rows["Server Configuration"]


def test_status_rows_report_an_unconfigured_host():
    rows = dict(
        settings_app.status_rows(
            _status(
                backend_candidate=None,
                credential=CredentialState(False, None),
                config=ServerConfig(False, None, None, False),
                service=ServiceState(False, False, False, "unit is not installed"),
            )
        )
    )
    assert rows["Backend"] == "None"
    assert rows["Viewer Credential"] == "Not set"
    assert rows["Server Configuration"] == "Not provisioned"
    assert rows["Service"] == "Not installed"


WLR_INTERFACES = (
    "zwlr_screencopy_manager_v1",
    "zwlr_virtual_pointer_manager_v1",
    "zwp_virtual_keyboard_manager_v1",
)


def _actions(tmp_path, *, session_type="wayland", enabled="enabled", interfaces=()):
    def run(args):
        stdout = {"is-enabled": enabled, "is-active": "active"}.get(args[0], "")
        return subprocess.CompletedProcess(args, 0, stdout, "")

    return settings.Actions(
        tmp_path,
        run=run,
        generate_key=lambda path: path.write_text("k", encoding="utf-8"),
        capabilities={
            "session_type": session_type,
            "interfaces": list(interfaces),
            "gnome_remote_desktop": False,
            "gnome_screencast": False,
            "kwin": False,
            "remote_desktop_portal": False,
            "screencast_portal": False,
            "binaries": {},
        },
        # No systemctl on this fake host: applying a bind stores it and finds
        # nothing running to restart, so the window tests never reach the system.
        # Its runner answers `ip` (the connect section) with a failure: no addresses.
        host=_offline_host(),
    )


def _offline_host():
    return runtime.Host(
        lambda _n: None,
        lambda *_a: None,
        lambda args, stdin=None: subprocess.CompletedProcess(args, 1, "", ""),
    )


def test_window_builds_from_real_state(display, tmp_path):
    adw, _ = settings_app._load_gtk()
    actions = _actions(tmp_path)
    actions.set_credential("vnc", "hunter2x")
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.Test")
    chosen = []
    window = settings_app.build_window(application, actions, on_choose=chosen.append)
    assert window.get_title() == settings_app.TITLE
    assert window.get_content() is not None


def test_window_builds_when_nothing_is_installed(display, tmp_path):
    adw, _ = settings_app._load_gtk()
    actions = _actions(tmp_path, session_type="x11", enabled="not-found")
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.Test2")
    window = settings_app.build_window(application, actions)
    assert window.get_content() is not None


@pytest.mark.parametrize("key,verb", [("enabled", "enable"), ("active", "start")])
def test_switches_drive_the_real_unit(tmp_path, key, verb):
    calls = []

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    actions = settings.Actions(tmp_path, run=run, generate_key=lambda p: None)
    failures = []
    settings_app._apply_switch(actions, key, True, failures.append)
    assert calls[-1] == [verb, settings.UNIT]
    assert failures == []


def test_a_failed_service_switch_is_reported_and_the_switch_falls_back(display, tmp_path):
    """systemctl refusing the action used to leave the switch showing a state the unit
    never reached, the error visible only on stderr. Now the window says why in a
    toast and, on the next idle, redraws the switch from the unit's real state."""

    def run(args):
        if args[0] in ("start", "stop", "enable", "disable"):
            return subprocess.CompletedProcess(args, 1, "", "unit refused to start")
        stdout = {"is-enabled": "enabled", "is-active": "inactive"}.get(args[0], "")
        return subprocess.CompletedProcess(args, 0, stdout, "")

    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.SwFail")
    application.register(None)
    actions = settings.Actions(
        tmp_path,
        run=run,
        generate_key=lambda p: p.write_text("k", encoding="utf-8"),
        capabilities=_actions(tmp_path).capabilities,
        host=_offline_host(),
    )
    actions.set_credential("vnc", "hunter2x")
    window = settings_app.build_window(application, actions)
    shown = []
    next(_rows(window, "ToastOverlay")).add_toast = lambda toast: shown.append(toast.get_title())
    running = next(r for r in _rows(window, "SwitchRow") if r.get_title() == "Running Now")
    assert running.get_sensitive() and not running.get_active()
    running.set_active(True)
    assert shown == ["unit refused to start"], "the failure reason is shown, not swallowed"
    _settle()
    redrawn = next(r for r in _rows(window, "SwitchRow") if r.get_title() == "Running Now")
    assert not redrawn.get_active(), "the redraw shows the state the unit is really in"


def test_main_wires_activate_to_a_window_without_a_main_loop(tmp_path):
    class FakeApplication:
        def __init__(self, application_id):
            self.application_id = application_id
            self.handlers = {}

        def connect(self, signal, handler):
            self.handlers[signal] = handler

        def run(self, argv):
            assert "activate" in self.handlers
            return 0

    created = {}
    assert (
        settings_app.main(
            [],
            application_factory=lambda application_id: created.setdefault(
                "app", FakeApplication(application_id)
            ),
            actions=_actions(tmp_path),
        )
        == 0
    )
    assert created["app"].application_id == settings_app.APP_ID


def test_the_app_follows_the_os_theme_instead_of_forcing_one(display, tmp_path):
    """The window must inherit the desktop's light/dark preference, never override it."""
    adw, _ = settings_app._load_gtk()
    manager = adw.StyleManager.get_default()
    # DEFAULT is libadwaita's "follow the system colour scheme" value.
    assert manager.get_color_scheme() == adw.ColorScheme.DEFAULT
    actions = _actions(tmp_path)
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.Theme")
    application.register(None)
    settings_app.build_window(application, actions)
    # Building the window must not have pinned the app to a scheme of its own.
    assert manager.get_color_scheme() == adw.ColorScheme.DEFAULT


def test_the_app_tracks_the_system_scheme_in_both_directions(display, tmp_path):
    """Forcing the scheme the way a desktop would must flip what the app renders."""
    adw, _ = settings_app._load_gtk()
    manager = adw.StyleManager.get_default()
    try:
        manager.set_color_scheme(adw.ColorScheme.FORCE_DARK)
        assert manager.get_dark()
        manager.set_color_scheme(adw.ColorScheme.FORCE_LIGHT)
        assert not manager.get_dark()
    finally:
        manager.set_color_scheme(adw.ColorScheme.DEFAULT)


def test_the_app_ships_no_hardcoded_colours():
    """No CSS or literal colour is baked in, so the desktop's palette always wins."""
    source = Path(settings_app.__file__).read_text(encoding="utf-8")
    assert "set_color_scheme" not in source, "the app must not pin a colour scheme"
    assert "CssProvider" not in source, "the app must not inject its own stylesheet"
    assert not re.search(r"#[0-9a-fA-F]{6}\b", source), "no hardcoded hex colours"
    assert "rgb(" not in source, "no hardcoded rgb() colours"


def _rows(root, type_name):
    """Every widget of a type in the tree (GTK4 has no container children API)."""
    child = root.get_first_child()
    while child is not None:
        if type(child).__name__ == type_name:
            yield child
        yield from _rows(child, type_name)
        child = child.get_next_sibling()


def test_activating_an_option_row_opens_its_dialog(display, tmp_path, monkeypatch):
    """With no hook injected, the rows dispatch to the real dialogs."""
    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.Rows")
    application.register(None)
    opened = []
    monkeypatch.setattr(settings_app, "open_for", lambda key, *_a, **_k: opened.append(key))
    window = settings_app.build_window(application, _actions(tmp_path))
    credential = next(
        r for r in _rows(window, "ActionRow") if r.get_title() == "Set Viewer Password"
    )
    credential.emit("activated")
    assert opened == ["credential"]


def test_main_binds_gtk_itself_when_no_factory_is_injected(monkeypatch, tmp_path):
    class FakeApplication:
        def __init__(self, application_id):
            self.application_id = application_id

        def connect(self, _signal, _handler):
            return None

        def run(self, _argv):
            return 0

    class FakeAdw:
        Application = FakeApplication

    monkeypatch.setattr(settings_app, "_load_gtk", lambda: (FakeAdw, None))
    assert settings_app.main([], actions=_actions(tmp_path)) == 0


def test_main_reports_missing_gtk_cleanly(monkeypatch, capsys):
    """No traceback, just an install hint and a non-zero exit."""
    import importlib

    real = importlib.import_module

    def missing(name, *args, **kwargs):
        if name == "gi":
            raise ImportError("No module named gi")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(settings_app.importlib, "import_module", missing)
    assert settings_app.main([]) == 2
    err = capsys.readouterr().err
    assert "needs GTK4 and libadwaita" in err
    assert "Traceback" not in err


def test_connect_section_lists_targets_with_copy_buttons(display, tmp_path, monkeypatch):
    """The section a phone user reads: name and LAN addresses with the port."""
    adw, gtk = settings_app._load_gtk()
    info = settings.ConnectInfo("zenbook", (("wlo1", "192.168.1.33"),), 5900)
    monkeypatch.setattr(settings_app, "connect_info", lambda _config, run=None: info)
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.Conn")
    application.register(None)
    window = settings_app.build_window(application, _actions(tmp_path))
    titles = [r.get_title() for r in _rows(window, "ActionRow")]
    assert "zenbook.local:5900" in titles
    assert "192.168.1.33:5900" in titles
    # Every target carries a copy button whose handler runs cleanly.
    buttons = [b for b in _rows(window, "Button") if b.get_icon_name() == "edit-copy-symbolic"]
    assert len(buttons) == 2
    buttons[1].emit("clicked")


def test_connect_section_explains_when_there_is_no_network(display, tmp_path, monkeypatch):
    adw, _ = settings_app._load_gtk()
    monkeypatch.setattr(
        settings_app, "connect_info", lambda _c, run=None: settings.ConnectInfo("zenbook", (), 5900)
    )
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.NoNet")
    application.register(None)
    window = settings_app.build_window(application, _actions(tmp_path))
    titles = [r.get_title() for r in _rows(window, "ActionRow")]
    assert "No local network address found" in titles


def _built_catalogues(tmp_path, *codes):
    """A locale tree holding real compiled catalogues for `codes`."""
    localedir = tmp_path / "locale"
    for code in codes:
        target = localedir / code / "LC_MESSAGES"
        target.mkdir(parents=True)
        source = Path(__file__).resolve().parent.parent / "po" / f"{code}.po"
        subprocess.run(
            ["msgfmt", "--output-file", str(target / "wayland-vnc.mo"), str(source)], check=True
        )
    return localedir


def test_a_stored_language_is_applied_before_the_first_frame(display, tmp_path, monkeypatch):
    """Opening must not flash English and then re-render in the chosen language."""
    localedir = _built_catalogues(tmp_path, "de")
    monkeypatch.setenv("WAYLAND_VNC_LOCALEDIR", str(localedir))
    i18n.write_language(tmp_path, "de")
    i18n.activate(i18n.AUTOMATIC)

    class FakeApplication:
        """Stands in for Adw.Application so `run` returns instead of entering a loop."""

        def __init__(self, application_id):
            self.application_id = application_id
            self.handlers = {}

        def connect(self, signal, handler):
            self.handlers[signal] = handler

        def run(self, argv):
            # main() must have chosen the language before anything could be drawn.
            assert i18n.active_language() == "de"
            return 0

    assert (
        settings_app.main(
            [],
            application_factory=lambda application_id: FakeApplication(application_id),
            actions=_actions(tmp_path),
        )
        == 0
    )


def test_the_app_never_overrides_the_system_accent_colour(display, tmp_path):
    """The accent must come from the desktop (GNOME Settings > Appearance), never be
    pinned by the app. libadwaita follows the settings portal on its own, so the only
    way to break this is to set it; assert we do not."""
    source = Path(settings_app.__file__).read_text(encoding="utf-8")
    assert "set_accent_color" not in source
    adw, _ = settings_app._load_gtk()
    manager = adw.StyleManager.get_default()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.Accent")
    application.register(None)
    before = manager.get_accent_color()
    settings_app.build_window(application, _actions(tmp_path))
    assert manager.get_accent_color() == before, "building the window changed the accent"


def test_the_primary_menu_offers_about_and_the_app_quits_on_ctrl_q(display, tmp_path):
    """About belongs in the header-bar primary menu, as the HIG specifies."""
    adw, gtk = settings_app._load_gtk()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.Menu")
    application.register(None)
    chosen = []
    window = settings_app.build_window(application, _actions(tmp_path), on_choose=chosen.append)
    button = next(_rows(window, "MenuButton"))
    assert button.get_icon_name() == "open-menu-symbolic"
    assert button.get_primary(), "F10 must open the primary menu"
    application.activate_action("about", None)
    assert chosen == ["about"]
    assert application.get_accels_for_action("app.quit") == ["<Control>q"]
    assert application.lookup_action("quit") is not None


def test_service_switches_are_adwaita_switch_rows(display, tmp_path):
    """Hand-built ActionRow+Switch renders differently from every other GNOME switch."""
    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.SwRow")
    application.register(None)
    window = settings_app.build_window(application, _actions(tmp_path))
    titles = [r.get_title() for r in _rows(window, "SwitchRow")]
    assert titles == ["Local Network Access", "Start at Login", "Running Now"]


def _lan_switch(window):
    return next(r for r in _rows(window, "SwitchRow") if r.get_title() == "Local Network Access")


def _settle():
    """Run the deferred redraws: the window rebuilds its page on the next idle, not
    inside the switch's own signal dispatch, so a test has to let that idle run."""
    import importlib

    glib = importlib.import_module("gi.repository.GLib")
    context = glib.MainContext.default()
    while context.iteration(False):
        pass


def test_the_local_network_switch_is_off_by_default_and_greys_the_addresses(
    display, tmp_path, monkeypatch
):
    """Loopback is the default, and the addresses a phone would use read as unreachable
    until the switch is on."""
    info = settings.ConnectInfo("zenbook", (("wlo1", "192.168.1.33"),), 5900)
    monkeypatch.setattr(settings_app, "connect_info", lambda _config, run=None: info)
    window = _window_for(tmp_path, "LanOff", interfaces=WLR_INTERFACES)
    switch = _lan_switch(window)
    assert switch.get_sensitive() and not switch.get_active()
    assert "this computer only" in switch.get_subtitle()
    address = next(r for r in _rows(window, "ActionRow") if r.get_title() == "192.168.1.33:5900")
    assert not address.get_sensitive()


def test_flipping_the_local_network_switch_applies_and_confirms(display, tmp_path, monkeypatch):
    """One flip stores the wildcard bind, restarts the server, toasts, and redraws with
    the addresses live; flipping back returns to this computer only."""
    info = settings.ConnectInfo("zenbook", (("wlo1", "192.168.1.33"),), 5900)
    monkeypatch.setattr(settings_app, "connect_info", lambda _config, run=None: info)
    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.LanFlip")
    application.register(None)
    actions = _actions(tmp_path, interfaces=WLR_INTERFACES)
    window = settings_app.build_window(application, actions)
    shown = []
    next(_rows(window, "ToastOverlay")).add_toast = lambda toast: shown.append(toast.get_title())
    _lan_switch(window).set_active(True)
    assert actions.status().config.lan_access, "applied at once, before any redraw"
    _settle()
    assert shown == ["Local network access turned on"]
    address = next(r for r in _rows(window, "ActionRow") if r.get_title() == "192.168.1.33:5900")
    assert address.get_sensitive()
    assert _lan_switch(window).get_active()
    _lan_switch(window).set_active(False)
    _settle()
    assert shown[-1] == "Local network access turned off"
    assert actions.status().config.loopback_only


def test_the_page_is_not_rebuilt_inside_a_switch_signal(display, tmp_path):
    """The contract behind the e2e abort: toggling the local-network slider must not
    rebuild the page inside that slider's own signal dispatch. In the containers a
    synchronous rebuild finalised the rows there and then, and the next toggle of a
    slider the script still held hit a dead AdwSwitchRow (assertion in
    adw-switch-row.c, process aborted). So: after the toggle every widget is still
    the one on screen; only once the caller lets the idle run is the page rebuilt."""
    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.LanGc")
    application.register(None)
    actions = _actions(tmp_path, interfaces=WLR_INTERFACES)
    window = settings_app.build_window(application, actions)
    # The inner sliders in page order: Local Network Access, Start at Login, Running Now.
    sliders = list(_rows(window, "Switch"))
    sliders[0].set_active(True)
    assert actions.status().config.lan_access, "stored and applied at once"
    assert sliders[1].get_root() is window, "the page on screen is still the same one"
    sliders[1].set_active(True)  # a live row, as any click before the idle would find
    _settle()
    assert sliders[1].get_root() is None, "the idle rebuilt the page"
    assert _lan_switch(window).get_active()


def test_the_local_network_switch_explains_itself_where_it_cannot_work(display, tmp_path):
    """GNOME with the distribution's daemon: insensitive, with the reason as subtitle."""
    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.LanNo")
    application.register(None)
    actions = settings.Actions(
        tmp_path,
        run=lambda args: subprocess.CompletedProcess(args, 0, "", ""),
        generate_key=lambda p: None,
        capabilities={
            "session_type": "wayland",
            "interfaces": [],
            "gnome_remote_desktop": True,
            "gnome_screencast": True,
            "kwin": False,
            "remote_desktop_portal": False,
            "screencast_portal": False,
            "binaries": {},
        },
        host=runtime.Host(
            lambda _n: None,
            lambda *_a: None,
            lambda args, stdin=None: subprocess.CompletedProcess(args, 1, "", ""),
            private_grd=lambda: False,
        ),
    )
    switch = _lan_switch(settings_app.build_window(application, actions))
    assert not switch.get_sensitive()
    assert "wayland-vnc-grd" in switch.get_subtitle()


def _window_for(tmp_path, suffix, **kw):
    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id=f"io.github.ventura8.wayland_vnc.{suffix}")
    application.register(None)
    return settings_app.build_window(application, _actions(tmp_path, **kw))


def test_blocking_state_is_a_banner_not_a_group_description(display, tmp_path):
    """Adw.Banner is the HIG widget for persistent, important state."""
    runtime.set_password(tmp_path, read_secret=lambda _p: "hunter2x")
    banner = next(_rows(_window_for(tmp_path, "BannerOff"), "Banner"))
    assert not banner.get_revealed(), "nothing blocks a usable service"
    banner = next(_rows(_window_for(tmp_path, "BannerOn", session_type="x11"), "Banner"))
    assert banner.get_revealed()
    assert "Wayland" in banner.get_title()


def test_committing_a_dialog_confirms_with_a_toast(display, tmp_path):
    """Transient confirmation is a toast, not a silent redraw."""
    window = _window_for(tmp_path, "Toast")
    overlay = next(_rows(window, "ToastOverlay"))
    shown = []
    overlay.add_toast = lambda toast: shown.append(toast.get_title())
    window.done("credential")
    window.done("network")
    window.done("diagnostic")  # read-only: nothing to confirm
    assert shown == ["Viewer password saved", "Bind address and port applied"]


def test_the_window_meets_the_adaptive_minimum_width(display, tmp_path):
    """The HIG expects windows to remain usable on a 360px-wide screen."""
    width, _height = _window_for(tmp_path, "Adaptive").get_size_request()
    assert width == 360


def test_the_banner_offers_the_fix_when_the_blocker_is_a_missing_password(display, tmp_path):
    """A banner with a fix carries the button for it (HIG); other blockers do not."""
    chosen = []
    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.BannerFix")
    application.register(None)
    window = settings_app.build_window(application, _actions(tmp_path), on_choose=chosen.append)
    banner = next(_rows(window, "Banner"))
    assert banner.get_revealed() and banner.get_button_label() == "Set Password"
    banner.emit("button-clicked")
    assert chosen == ["credential"]
    x11 = next(_rows(_window_for(tmp_path, "BannerNoFix", session_type="x11"), "Banner"))
    assert x11.get_revealed() and not x11.get_button_label(), "X11 has no in-app fix"


def test_keyboard_shortcuts_are_offered_where_libadwaita_has_the_dialog(display, tmp_path):
    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.Keys")
    application.register(None)
    settings_app.build_window(application, _actions(tmp_path))
    if hasattr(adw, "ShortcutsDialog"):
        assert application.lookup_action("shortcuts") is not None
        assert application.get_accels_for_action("app.shortcuts") == ["<Control>question"]
        dialog = settings_app.shortcuts_dialog(adw)
        assert type(dialog).__name__ == "ShortcutsDialog"
    else:
        assert application.lookup_action("shortcuts") is None, "never a dead menu item"


def _language_chooser(tmp_path, suffix):
    from wayland_vnc import settings_dialogs

    adw, gtk = settings_app._load_gtk()
    application = adw.Application(application_id=f"io.github.ventura8.wayland_vnc.{suffix}")
    application.register(None)
    chosen = []
    actions = _actions(tmp_path)
    window = settings_app.build_window(application, actions)
    dialog = settings_dialogs.language_dialog(
        settings_dialogs.Toolkit(adw, gtk), actions, chosen.append
    )
    return window, dialog, chosen


def _menu_labels(window):
    menu = next(_rows(window, "MenuButton")).get_menu_model()
    return [
        menu.get_item_attribute_value(i, "label", None).get_string()
        for i in range(menu.get_n_items())
    ]


def test_the_primary_menu_is_relabelled_when_the_language_changes(display, tmp_path, monkeypatch):
    """A Gio.Menu keeps the strings it was built with, so the switch must rebuild it:
    the menu and its tooltip are in the chosen language, and back in English after
    Automatic."""
    localedir = _built_catalogues(tmp_path, "ro")
    monkeypatch.setenv("WAYLAND_VNC_LOCALEDIR", str(localedir))
    window, _dialog, _chosen = _language_chooser(tmp_path, "MenuLang")
    assert "About Wayland VNC" in _menu_labels(window)
    window.relanguage("ro")
    assert "Despre Wayland VNC" in _menu_labels(window)
    assert "About Wayland VNC" not in _menu_labels(window)
    assert next(_rows(window, "MenuButton")).get_tooltip_text() == "Meniu principal"
    window.relanguage(i18n.AUTOMATIC)
    assert "About Wayland VNC" in _menu_labels(window)
    window.close()


def test_the_language_row_shows_the_current_choice_and_opens_the_chooser(
    display, tmp_path, monkeypatch
):
    """A navigation row, not a combo: ~100 entries belong in the HIG list-with-search
    chooser, as in GNOME Settings."""
    localedir = _built_catalogues(tmp_path, "ro", "ja")
    monkeypatch.setenv("WAYLAND_VNC_LOCALEDIR", str(localedir))
    chosen = []
    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.LangRow")
    application.register(None)
    window = settings_app.build_window(application, _actions(tmp_path), on_choose=chosen.append)
    row = next(r for r in _rows(window, "ActionRow") if r.get_title() == "Language")
    assert row.get_subtitle().startswith("Automatic"), "a fresh install follows the desktop"
    assert row.get_activatable() and row.get_sensitive()
    row.emit("activated")
    assert chosen == ["language"]


def test_the_chooser_lists_automatic_first_marks_the_current_and_applies_on_activate(
    display, tmp_path, monkeypatch
):
    localedir = _built_catalogues(tmp_path, "ro", "ja")
    monkeypatch.setenv("WAYLAND_VNC_LOCALEDIR", str(localedir))
    window, dialog, chosen = _language_chooser(tmp_path, "LangDlg")
    codes = list(dialog.rows)
    assert codes[0] == i18n.AUTOMATIC and codes[1:] == ["ja", "ro"], "installed, by code"
    assert [dialog.rows[c].get_title() for c in codes[1:]] == ["日本語", "Română"], "endonyms"
    assert dialog.rows[i18n.AUTOMATIC].check.get_visible(), "the current choice is checked"
    assert not dialog.rows["ro"].check.get_visible()
    dialog.rows["ro"].emit("activated")
    assert chosen == ["ro"], "activating a row applies it"
    window.close()


def test_the_chooser_search_filters_by_endonym_or_code_with_an_empty_state(
    display, tmp_path, monkeypatch
):
    """The HIG search pattern for a chooser: entry on top, live filtering, empty state."""
    localedir = _built_catalogues(tmp_path, "ro", "ja", "de")
    monkeypatch.setenv("WAYLAND_VNC_LOCALEDIR", str(localedir))
    _window, dialog, _chosen = _language_chooser(tmp_path, "LangSearch")
    assert dialog.search_entry.get_placeholder_text() == "Search languages"

    dialog.search_entry.set_text("rom")
    dialog.search_entry.emit("search-changed")
    assert dialog.listbox.get_visible()
    dialog.search_entry.set_text("ja")  # matches the code, not only the endonym
    dialog.search_entry.emit("search-changed")
    assert dialog.listbox.get_visible()
    for query in ("romana", "ROMANA", "Română", "ROMÂNĂ"):  # accents and case never matter
        dialog.search_entry.set_text(query)
        dialog.search_entry.emit("search-changed")
        assert dialog.listbox.get_visible(), query
        assert dialog.listbox.matches(dialog.rows["ro"]), query
        assert not dialog.listbox.matches(dialog.rows["de"]), query
    dialog.search_entry.set_text("zzzz")
    dialog.search_entry.emit("search-changed")
    assert not dialog.listbox.get_visible(), "nothing matches: the list yields to the empty state"
    empty = next(w for w in _rows(dialog.get_child(), "StatusPage"))
    assert empty.get_visible() and empty.get_title() == "No Results Found"
    dialog.search_entry.set_text("")
    dialog.search_entry.emit("search-changed")
    assert dialog.listbox.get_visible() and not empty.get_visible()


def test_language_row_is_insensitive_with_a_reason_when_nothing_is_installed(
    display, tmp_path, monkeypatch
):
    """Never a dead control: with no catalogues the row is insensitive and explains."""
    monkeypatch.setenv("WAYLAND_VNC_LOCALEDIR", str(tmp_path / "empty"))
    window = _window_for(tmp_path, "LangNone")
    row = next(r for r in _rows(window, "ActionRow") if r.get_title() == "Language")
    assert not row.get_sensitive()
    groups = [g for g in _rows(window, "PreferencesGroup") if g.get_title() == "Language"]
    assert "No translations are installed" in groups[0].get_description()
