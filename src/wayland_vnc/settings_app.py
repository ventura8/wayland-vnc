"""Native GTK4/libadwaita settings window for wayland-vnc.

libadwaita is used throughout so the window adopts the host desktop's theme, accent
and light/dark preference and reads as a first-party application of that OS rather
than a custom-skinned tool.

The window renders only real state gathered by `settings.gather`, and every control
is backed by a real action in `settings.Actions`. A control whose preconditions are
not met is rendered insensitive with the reason shown, never as a dead toggle.
GTK is imported lazily so the CLI and tests can import this module cheaply.
"""

import importlib
import sys
from collections.abc import Callable

from wayland_vnc import i18n, runtime
from wayland_vnc.i18n import _
from wayland_vnc.settings import Actions, ConnectInfo, Option, ServerConfig, Status, connect_info
from wayland_vnc.settings_dialogs import Toolkit, language_dialog, open_for

APP_ID = "io.github.ventura8.wayland_vnc.Settings"
TITLE = "Wayland VNC"
# What each committing dialog confirms with a toast; resolved at show time so the
# text follows the active language.
TOASTS = {
    "credential": lambda: _("Viewer password saved"),
    "network": lambda: _("Bind address and port applied"),
    "lan-on": lambda: _("Local network access turned on"),
    "lan-off": lambda: _("Local network access turned off"),
}


# The window is built from Adw.Dialog (libadwaita 1.5), Adw.SpinRow and
# Adw.ToolbarView (1.4), and Adw.PasswordEntryRow (1.2). 1.5 is therefore the floor,
# and libadwaita 1.5 itself requires GTK 4.14.
MINIMUM_ADWAITA = (1, 5)


def require_adwaita(adw, minimum: tuple[int, int] = MINIMUM_ADWAITA) -> None:
    """Refuse an libadwaita too old for the widgets this window is made of.

    Without this the window builds until it reaches the first missing widget and dies
    with an AttributeError naming an internal symbol, which tells the reader nothing
    about what to install.
    """
    found = (adw.get_major_version(), adw.get_minor_version())
    if found < minimum:
        raise RuntimeError(
            _("The settings window needs libadwaita %(needed)s or newer; this system has %(found)s")
            % {
                "needed": ".".join(str(part) for part in minimum),
                "found": ".".join(str(part) for part in found),
            }
        )


def _load_gtk():
    """Bind the GTK 4 / libadwaita typelibs.

    The versions must be selected before the typelib modules are loaded, so the
    modules are resolved through importlib rather than a top-level `from` import
    that would run too early.
    """
    try:
        gi = importlib.import_module("gi")
    except ImportError as error:  # the GUI is optional; the CLI and service do not need it
        raise RuntimeError(
            _(
                "The settings window needs GTK4 and libadwaita "
                "(install python3-gi, gir1.2-gtk-4.0 and gir1.2-adw-1)"
            )
        ) from error
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    adw = importlib.import_module("gi.repository.Adw")
    gtk = importlib.import_module("gi.repository.Gtk")
    require_adwaita(adw)
    # Both inits are idempotent; calling them here means widgets can be constructed
    # (and asserted on) without first entering the application's main loop.
    gtk.init_check()
    adw.init()
    return adw, gtk


def _yes_no(value: bool) -> str:
    return _("Yes") if value else _("No")


def _scope_text(scope: str) -> str:
    return {
        "loopback": _("this machine only"),
        "any-network": _("every network this computer is on"),
        "local-network": _("local network only"),
    }.get(scope, _("PUBLIC ADDRESS - reachable beyond the local network"))


def status_rows(status: Status, *, private_grd: bool = False) -> list[tuple[str, str]]:
    """The status view as (title, subtitle) pairs, derived only from gathered state.

    `private_grd` says whether the private GNOME Remote Desktop build is installed:
    only then does the stored bind describe where that daemon listens.
    """
    credential = status.credential
    config = status.config
    service = status.service
    if credential.present:
        secure = (
            _("mode 600") if credential.secure_mode else _("mode %s (too open)") % credential.mode
        )
        credential_text = _("Stored (%s)") % secure
    else:
        credential_text = _("Not set")
    if status.backend_candidate == "grd" and private_grd:
        # The private daemon listens where the stored bind says (loopback until local
        # network access is turned on), always on 5900, always with a password.
        address = config.address if config.present else runtime.DEFAULT_ADDRESS
        config_text = _("%(address)s:%(port)s (%(scope)s), auth %(auth)s") % {
            "address": address,
            "port": runtime.GRD_VNC_PORT,
            "scope": _scope_text(ServerConfig(True, address, None, True).scope),
            "auth": _yes_no(True),
        }
    elif status.backend_candidate == "grd":
        config_text = _(
            "Served by GNOME Remote Desktop VNC (port 5900); exposure follows "
            "GNOME Settings > Sharing"
        )
    elif config.present:
        config_text = _("%(address)s:%(port)s (%(scope)s), auth %(auth)s") % {
            "address": config.address,
            "port": config.port,
            "scope": _scope_text(config.scope),
            "auth": _yes_no(config.auth_enabled),
        }
    else:
        config_text = _("Not provisioned")
    return [
        (_("Backend"), status.backend_candidate or _("None")),
        (_("Session"), status.session_type),
        (_("Capability Verdict"), _(status.reason)),
        (_("Viewer Credential"), credential_text),
        (_("Server Configuration"), config_text),
        (_("Service"), _(service.detail) if service.installed else _("Not installed")),
    ]


def _status_group(adw, status: Status, *, private_grd: bool):
    group = adw.PreferencesGroup(title=_("Status"))
    for title, subtitle in status_rows(status, private_grd=private_grd):
        group.add(adw.ActionRow(title=title, subtitle=subtitle, subtitle_selectable=True))
    return group


def _copy_button(gtk, text: str):
    """A suffix button that puts `text` on the clipboard for a desktop viewer."""
    button = gtk.Button(icon_name="edit-copy-symbolic", valign=gtk.Align.CENTER)
    button.add_css_class("flat")
    button.set_tooltip_text(_("Copy"))
    button.connect("clicked", lambda b: b.get_clipboard().set(text))
    return button


def _lan_switch(adw, lan: Option, lan_on: bool, on_choose: Callable):
    """The opt-in itself: off is this computer only (the default), on is every network
    this computer is on. Where the backend cannot honour it the row is insensitive
    and its subtitle says why, so it never reads as a choice that was made."""
    switch = adw.SwitchRow(
        title=lan.label,
        subtitle=lan.reason
        or _("Let devices on the same Wi-Fi or LAN connect. Off: this computer only."),
        active=lan_on,
    )
    switch.set_sensitive(lan.available)
    # Connected after construction, so drawing the stored state is never a choice.
    switch.connect(
        "notify::active", lambda row, _p: on_choose("lan-on" if row.get_active() else "lan-off")
    )
    return switch


def _connect_group(adw, gtk, info: ConnectInfo, switch):
    """What to type into RealVNC Viewer on a phone or another computer, behind the
    one switch that decides whether such a device can connect at all."""
    group = adw.PreferencesGroup(
        title=_("Connect from Another Device"),
        description=_(
            "In RealVNC Viewer, add a connection to one of these. They work while local "
            "network access is on, for any device on the same network. Do not forward "
            "the port from the Internet."
        ),
    )
    group.add(switch)
    for label, target in info.targets:
        row = adw.ActionRow(title=target, subtitle=label, title_selectable=True)
        row.add_suffix(_copy_button(gtk, target))
        # Greyed while nothing but this computer can reach them.
        row.set_sensitive(switch.get_active())
        group.add(row)
    if not info.addresses:
        group.add(
            adw.ActionRow(
                title=_("No local network address found"),
                subtitle=_("Connect this machine to Wi-Fi or Ethernet, then reopen"),
            )
        )
    return group


def _options_group(adw, gtk, actions: Actions, status: Status, on_choose: Callable):
    """One row per real option; unavailable ones are insensitive and state the reason."""
    group = adw.PreferencesGroup(
        title=_("Configuration"),
        description=_("Every option here changes real configuration or the installed service."),
    )
    for option in actions.options(status):
        row = adw.ActionRow(title=option.label, subtitle=option.reason, activatable=True)
        row.set_sensitive(option.available)
        row.add_suffix(gtk.Image(icon_name="go-next-symbolic"))
        row.connect("activated", lambda _row, key=option.key: on_choose(key))
        group.add(row)
    return group


def _service_group(adw, actions: Actions, status: Status, on_error: Callable):
    """Enable/active switches that drive systemd --user, insensitive when unusable.

    These switches are the only place the service is controlled, so when they are not
    usable the group itself must say why -- there is no separate row to carry it.
    """
    usable = service_usable(status)
    # The reason the switches are unusable is shown in the Adw.Banner above the page,
    # the HIG's widget for persistent blocking state, rather than as a description.
    group = adw.PreferencesGroup(title=_("Service"))
    for key, title, value in (
        ("enabled", _("Start at Login"), status.service.enabled),
        ("active", _("Running Now"), status.service.active),
    ):
        # Adw.SwitchRow is libadwaita's own row-with-a-switch (1.4+, below this
        # window's 1.5 floor); a hand-built ActionRow plus Gtk.Switch renders
        # subtly differently from every other GNOME settings switch.
        row = adw.SwitchRow(title=title, active=value)
        row.set_sensitive(usable)
        row.connect(
            "notify::active",
            lambda r, _p, k=key: _apply_switch(actions, k, r.get_active(), on_error),
        )
        group.add(row)
    return group


def _apply_switch(actions: Actions, key: str, state: bool, on_error: Callable) -> None:
    """Drive the real unit behind a switch row.

    A failed systemctl used to leave the switch showing the state it never reached
    (the exception only went to stderr). It is reported through `on_error`, whose
    redraw from real state puts the switch back where the unit really is.
    """
    try:
        if key == "enabled":
            actions.set_enabled(state)
        else:
            actions.set_active(state)
    except (RuntimeError, OSError) as error:
        on_error(error)


def _language_group(adw, gtk, actions: Actions, on_open: Callable[[], None]):
    """Choose the interface language, or follow the desktop's own.

    Automatic is the default and the first entry: a desktop already knows what
    language its owner reads, so the window matches the rest of the session unless
    someone deliberately picks otherwise. Only languages whose catalogue is actually
    installed are offered -- an entry that silently did nothing would be a dead
    control, and the group says so plainly when none are present.
    """
    installed = i18n.available()
    group = adw.PreferencesGroup(
        title=_("Language"),
        description=(
            _("The window follows your desktop's language unless you choose another.")
            if installed
            else _("No translations are installed, so the window stays in English.")
        ),
    )
    current = i18n.read_language(actions.directory)
    # A navigation row showing the current choice; ~100 languages are chosen in the
    # HIG's list-with-search chooser (language_dialog), not in a combo popover.
    row = adw.ActionRow(
        title=_("Language"),
        subtitle=(
            i18n.LANGUAGES.get(current)
            if current in i18n.LANGUAGES
            else _("Automatic (match the desktop)")
        ),
        activatable=True,
    )
    row.add_suffix(gtk.Image(icon_name="go-next-symbolic"))
    row.set_sensitive(bool(installed))
    row.connect("activated", lambda _row: on_open())
    group.add(row)
    return group


def service_usable(status: Status) -> bool:
    """Whether the service switches can do anything on this desktop right now."""
    return status.service.installed and status.credential.present and status.wayland


def _page(adw, gtk, actions: Actions, on_choose: Callable, on_error: Callable):
    """Gather state afresh and render it; called again after every change."""
    status = actions.status()
    page = adw.PreferencesPage()
    page.add(_status_group(adw, status, private_grd=actions.host.private_grd()))
    switch = _lan_switch(
        adw, actions.lan_access_option(status), status.config.lan_access, on_choose
    )
    # Through the host boundary the Actions were built with: a test's fake host
    # answers here, where a bare default once ran the real `ip` on the developer's box.
    info = connect_info(status.config, run=actions.host.run)
    page.add(_connect_group(adw, gtk, info, switch))
    page.add(_options_group(adw, gtk, actions, status, on_choose))
    page.add(_service_group(adw, actions, status, on_error))
    page.add(_language_group(adw, gtk, actions, lambda: on_choose("language")))
    return page


SHORTCUTS = (
    ("<Control>question", lambda: _("Keyboard shortcuts")),
    ("F10", lambda: _("Open the main menu")),
    ("<Control>q", lambda: _("Quit")),
)


def shortcuts_dialog(adw):
    """The standard Keyboard Shortcuts window (Adw.ShortcutsDialog, libadwaita 1.8)."""
    dialog = adw.ShortcutsDialog()
    section = adw.ShortcutsSection(title=_("General"))
    for accelerator, title in SHORTCUTS:
        section.add(adw.ShortcutsItem(title=title(), accelerator=accelerator))
    dialog.add(section)
    return dialog


def _primary_menu(
    kit: Toolkit,
    application,
    view,
    *,
    on_about: Callable[[], None],
    on_shortcuts: Callable[[], None] | None = None,
) -> Callable[[], None]:
    """The header bar with its primary menu, where the HIG places About and Keyboard
    Shortcuts; added to `view` as its first top bar.

    `primary=True` is what makes F10 open it, as in every GNOME application; Ctrl+Q
    quits and Ctrl+? opens the shortcuts window, both standard. Returns `relabel`,
    which rewrites the item labels and tooltip in the active language: a Gio.Menu
    keeps the strings it was built with, so a language switch must rebuild it.
    """
    header = kit.adw.HeaderBar()
    view.add_top_bar(header)
    # Bound here rather than passed in: the menu is the only place Gio is needed.
    gio = importlib.import_module("gi.repository.Gio")
    menu = gio.Menu()
    handlers = {"about": on_about, "quit": application.quit}
    if on_shortcuts is not None:
        handlers["shortcuts"] = on_shortcuts
    button = kit.gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True)
    header.pack_end(button)

    def relabel() -> None:
        menu.remove_all()
        if on_shortcuts is not None:
            # Adw.ShortcutsDialog arrived in libadwaita 1.8, above this window's 1.5
            # floor; the entry appears only where the dialog exists, never dead.
            menu.append(_("Keyboard Shortcuts"), "app.shortcuts")
        menu.append(_("About Wayland VNC"), "app.about")
        button.set_tooltip_text(_("Main Menu"))

    relabel()
    for name, handler in handlers.items():
        action = gio.SimpleAction(name=name)
        action.connect("activate", lambda _a, _p, h=handler: h())
        application.add_action(action)
    application.set_accels_for_action("app.quit", ["<Control>q"])
    application.set_accels_for_action("app.shortcuts", ["<Control>question"])
    return relabel


def _on_next_idle(callback: Callable[[], None]) -> None:
    """Run `callback` once, on the main loop's next idle, then let the source go."""
    glib = importlib.import_module("gi.repository.GLib")

    def run() -> bool:
        callback()
        return False

    glib.idle_add(run)


def build_window(application, actions: Actions, *, on_choose: Callable | None = None):
    """Construct the settings window from real, freshly gathered state.

    Option rows open their libadwaita dialogs, and the page is re-rendered from real
    state once a dialog commits, so what is shown never drifts from what is on disk.
    A caller may still inject `on_choose` to observe or replace that behaviour.
    """
    adw, gtk = _load_gtk()
    view = adw.ToolbarView()
    # The header bar goes in first, so the banner sits under it. Its menu's handlers
    # are closures over `choose` and `window`, which exist by the time anyone clicks.
    relabel_menu = _primary_menu(
        Toolkit(adw, gtk),
        application,
        view,
        on_about=lambda: choose("about"),
        on_shortcuts=(lambda: shortcuts_dialog(adw).present(window))
        if hasattr(adw, "ShortcutsDialog")
        else None,
    )
    # Adw.Banner: persistent, important state (why the service cannot run) belongs in
    # a banner under the header bar, not in a group description a reader can miss.
    banner = adw.Banner(revealed=False)
    view.add_top_bar(banner)
    # Adw.ToastOverlay: confirmations ("Viewer password saved") are transient
    # feedback, which the HIG delivers as toasts rather than by silently redrawing.
    overlay = adw.ToastOverlay(child=view)
    window = adw.ApplicationWindow(application=application, title=TITLE)
    window.set_default_size(560, 720)
    # The HIG's adaptive floor: usable on a 360px-wide phone screen.
    window.set_size_request(360, 294)
    window.set_content(overlay)

    def refresh() -> None:
        status = actions.status()
        banner.set_title(actions.service_reason(status))
        banner.set_revealed(not service_usable(status))
        fixable = not status.credential.present and status.service.installed and status.wayland
        banner.set_button_label(_("Set Password") if fixable else "")
        view.set_content(_page(adw, gtk, actions, choose, report))
        relabel_menu()

    def done(key: str):
        """Confirm a committed change with a toast, then redraw from real state."""
        text = TOASTS.get(key)
        if text is not None:
            overlay.add_toast(adw.Toast(title=text()))
        refresh()

    def report(error: Exception) -> None:
        """A failed action: say why in a toast, and redraw from what is really so on
        the next idle (never inside the failing widget's own signal dispatch)."""
        overlay.add_toast(adw.Toast(title=_(str(error))))
        _on_next_idle(refresh)

    def relanguage(language: str) -> None:
        """Store the choice, switch catalogue, and redraw every string in place."""
        i18n.write_language(actions.directory, language)
        i18n.activate(language)
        gtk.Widget.set_default_direction(
            gtk.TextDirection.RTL if i18n.is_rtl(language) else gtk.TextDirection.LTR
        )
        refresh()

    def choose(key: str) -> None:
        if on_choose is not None:
            on_choose(key)
            return
        if key == "language":
            language_dialog(Toolkit(adw, gtk), actions, relanguage).present(window)
            return
        if key in ("lan-on", "lan-off"):
            # The local-network switch: store and apply now, then confirm and
            # redraw on the next idle. The redraw disposes the page the switch sits
            # in, and this runs inside that switch's own notify::active dispatch:
            # rebuilding synchronously finalised the row under libadwaita's slider
            # callback and aborted the process. On failure the toast says why and
            # the deferred redraw puts the switch back where the stored bind is.
            try:
                actions.set_lan_access(key == "lan-on")
            except (ValueError, OSError, RuntimeError) as error:
                report(error)
                return
            _on_next_idle(lambda: done(key))
            return
        open_for(key, Toolkit(adw, gtk), actions, window, lambda: done(key))

    # The HIG's banner carries the fix when there is one: a missing credential is
    # resolved by the very dialog the window already has.
    banner.connect("button-clicked", lambda _b: choose("credential"))

    window.refresh = refresh
    window.done = done
    window.relanguage = relanguage
    refresh()
    return window


def main(argv: list[str] | None = None, *, application_factory=None, actions=None) -> int:
    """Run the settings application; factories are injectable so the wiring is testable."""
    if application_factory is None:
        try:
            adw, _ = _load_gtk()
        except RuntimeError as error:
            # The GUI is optional: report what to install, without an import traceback.
            print(str(error), file=sys.stderr)
            return 2
        application_factory = adw.Application
    application = application_factory(application_id=APP_ID)
    resolved = Actions() if actions is None else actions
    # Pick up the stored language before any widget is built, so the first frame is
    # already in the right language rather than flashing English and re-rendering.
    i18n.activate(i18n.read_language(resolved.directory))
    application.connect("activate", lambda app: build_window(app, resolved).present())
    return application.run(argv)
