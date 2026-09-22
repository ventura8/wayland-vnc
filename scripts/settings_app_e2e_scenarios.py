"""End-to-end scenarios for the packaged GTK settings app.

Runs against the STAGED payload (the tree stage-payload.sh installs), not the source
checkout, so packaging, the launcher's module resolution, the GTK widget tree, the
runtime and the real filesystem are all exercised together.

The window is driven the way a user drives it: real widgets are located in the live
widget tree and their signals are emitted, so GTK dispatches into the real handlers,
which write real files and invoke a fake `systemctl` placed on PATH. Nothing touches
the host's services.
"""

import gettext as _gettext
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

# The settings window's row titles, as the widget tree spells them.
CREDENTIAL_ROW = "Viewer Credential"
CONFIG_ROW = "Server Configuration"
LAN_ROW = "Local Network Access"
AUTOSTART_ROW = "Start at Login"
RUNNING_ROW = "Running Now"

FAILURES = []


def check(condition, message):
    if condition:
        print(f"  ok: {message}")
    else:
        print(f"  FAIL: {message}")
        FAILURES.append(message)


def walk(widget):
    """Yield every widget in the tree (GTK4 has no container children API)."""
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from walk(child)
        child = child.get_next_sibling()


def widgets_of(root, type_name):
    return [w for w in walk(root) if type(w).__name__ == type_name]


def systemctl_log(work):
    path = work / "systemctl.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def fake_systemctl(work, *, exit_code=0):
    """A systemctl stand-in that records argv and reports a unit that exists."""
    bindir = work / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "systemctl"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{work}/systemctl.log"\n'
        'case "$2" in\n'
        "  is-enabled) echo disabled ;;\n"
        "  is-active) echo inactive ;;\n"
        "esac\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"


def _window_scenarios(
    actions, adw, application, settings, settings_app, wayland_capabilities, work, _gtk
):
    """The scenarios that need a real window: the rendered page, the live switches,
    every dialog, and the diagnostics view."""
    print("== happy: the real window builds and renders the provisioned state ==")
    window = settings_app.build_window(application, actions)
    check(window.get_mapped() or window.get_content() is not None, "window content built")
    labels = [
        w.get_title()
        for w in walk(window)
        if type(w).__name__ == "ActionRow" and hasattr(w, "get_title")
    ]
    check(CREDENTIAL_ROW in labels, "status rows are present in the live widget tree")
    check("Set Viewer Password" in labels, "configuration rows are present in the widget tree")

    print("== happy: the connect section shows name and LAN addresses with the port ==")
    titles = [w.get_title() for w in walk(window) if type(w).__name__ == "ActionRow"]
    info = settings.connect_info(actions.status().config)
    check(
        f"{info.hostname}.local:{info.port}" in titles,
        "the mDNS name with the port is offered to connect from another device",
    )
    for iface, ip in info.addresses:
        check(f"{ip}:{info.port}" in titles, f"the {iface} address is listed with the port")
    check(
        not any(a[0].startswith(("docker", "lxc", "virbr")) for a in info.addresses),
        "virtual bridges a phone cannot reach are hidden",
    )

    def switch_rows():
        return {r.get_title(): r for r in widgets_of(window, "SwitchRow")}

    def settle():
        # The window redraws on the next idle, never inside a switch's own signal
        # dispatch; let that idle run before looking at the rebuilt page.
        from gi.repository import GLib

        context = GLib.MainContext.default()
        while context.iteration(False):
            # Each call runs one pending idle; there is nothing else to do here.
            continue

    print("== happy: toggling the live switches drives the real unit ==")
    rows = switch_rows()
    check(
        list(rows) == [LAN_ROW, AUTOSTART_ROW, RUNNING_ROW],
        "the local-network switch and both service switches exist in the window",
    )
    for title in (AUTOSTART_ROW, RUNNING_ROW):
        check(rows[title].get_sensitive(), "service switch enabled once preconditions are met")
    rows[AUTOSTART_ROW].set_active(True)
    rows[RUNNING_ROW].set_active(True)
    log = systemctl_log(work)
    check(any("enable wayland-vnc.service" in line for line in log), "switch ran enable")
    check(any("start wayland-vnc.service" in line for line in log), "switch ran start")
    rows[AUTOSTART_ROW].set_active(False)
    check(
        any("disable wayland-vnc.service" in line for line in systemctl_log(work)),
        "switching off ran systemctl disable",
    )

    print("== happy: the local-network switch opens and closes the LAN, live ==")
    lan = switch_rows()[LAN_ROW]
    check(lan.get_sensitive() and not lan.get_active(), "the switch is usable, off by default")
    # Toggle the slider inside the row, as a click does, holding no reference to the
    # row itself: the redraw that follows must not finalise it mid-signal.
    inner = [w for w in walk(lan) if type(w).__name__ == "Switch"][0]
    del lan
    inner.set_active(True)
    check(actions.status().config.lan_access, "the wildcard bind is stored at once")
    settle()
    lan = switch_rows()[LAN_ROW]
    check(lan.get_active(), "the redrawn switch shows local network access on")
    addresses = [
        w
        for w in widgets_of(window, "ActionRow")
        if w.get_title() == f"{info.hostname}.local:{info.port}"
    ]
    check(addresses and addresses[0].get_sensitive(), "the addresses read as reachable")
    lan.set_active(False)
    settle()
    check(actions.status().config.loopback_only, "off again: this machine only")
    lan = switch_rows()[LAN_ROW]
    check(not lan.get_active(), "the redrawn switch shows local network access off")

    print("== happy: the credential dialog writes through the runtime ==")
    from wayland_vnc import settings_dialogs

    kit = settings_dialogs.Toolkit(adw, _gtk)
    fresh = work / "dialog-config"
    dialog_actions = settings.Actions(
        fresh,
        generate_key=lambda path: path.write_text("k", encoding="utf-8"),
        capabilities=wayland_capabilities,
    )
    committed = []
    form = settings_dialogs.credential_form(kit, dialog_actions, lambda: committed.append(1))
    form.rows["username"].set_text("vnc")
    form.rows["password"].set_text("dialogpw1")
    form.rows["confirm"].set_text("dialogpw1")
    check(form.submit() is True, "credential dialog accepted a matching password")
    cred = fresh / "credentials"
    check(
        cred.exists() and format(cred.stat().st_mode & 0o777, "03o") == "600",
        "credential dialog wrote a mode-600 file",
    )
    check(committed == [1], "credential dialog triggered the window refresh")

    print("== bad: the credential dialog refuses a mismatch and writes nothing ==")
    bad = settings_dialogs.credential_form(
        kit,
        settings.Actions(
            work / "dialog-bad", generate_key=lambda p: None, capabilities=wayland_capabilities
        ),
        lambda: None,
    )
    bad.rows["password"].set_text("dialogpw1")
    bad.rows["confirm"].set_text("nope")
    check(bad.submit() is False, "mismatched confirmation refused")
    check(
        bad.error.get_visible() and "did not match" in bad.error.get_text(),
        "mismatch reason shown in the dialog",
    )
    check(not (work / "dialog-bad" / "credentials").exists(), "nothing written on mismatch")

    print("== bad: the credential dialog surfaces the weak-password rule ==")
    weak = settings_dialogs.credential_form(
        kit,
        settings.Actions(
            work / "dialog-weak", generate_key=lambda p: None, capabilities=wayland_capabilities
        ),
        lambda: None,
    )
    weak.rows["password"].set_text("abc")
    weak.rows["confirm"].set_text("abc")
    check(
        weak.submit() is False and "6-64" in weak.error.get_text(),
        "weak password refused with the runtime's own rule",
    )

    print("== happy: the network dialog provisions a loopback config ==")
    net = settings_dialogs.network_form(kit, dialog_actions, lambda: None)
    net.rows["address"].set_text("127.0.0.1")
    net.rows["port"].set_value(5902)
    check(net.submit() is True, "network dialog applied the bind")
    conf = (fresh / "wayvnc.conf").read_text(encoding="utf-8")
    check(
        "address=127.0.0.1" in conf and "port=5902" in conf,
        "network dialog wrote the loopback address and port",
    )

    print("== bad: the network dialog cannot select a privileged port ==")
    net.rows["port"].set_value(80)
    check(net.rows["port"].get_value() >= 1025, "port row clamps privileged ports away")

    print("== happy: the diagnostics dialog shows collapsible categories ==")
    diag = settings_dialogs.diagnostic_dialog(kit, dialog_actions)
    expanders = [w for w in walk(diag.get_child()) if type(w).__name__ == "ExpanderRow"]
    titles = [e.get_title() for e in expanders]
    for expected in ("Verdict", "Session", "Wayland Protocols", "Portals", "Binaries"):
        check(expected in titles, f"diagnostics has a '{expected}' category")
    verdict = next(e for e in expanders if e.get_title() == "Verdict")
    check(verdict.get_expanded(), "the verdict category is open by default")

    print("== happy: the service is controlled only by its switches ==")
    keys = [option.key for option in dialog_actions.options(dialog_actions.status())]
    check("service" not in keys, "no option row duplicates the Service switches")
    check("diagnostic" in keys, "the diagnostics row is present")
    labels = {
        option.key: option.label for option in dialog_actions.options(dialog_actions.status())
    }
    check(labels["diagnostic"] == "Diagnostics", "the diagnostics row is named 'Diagnostics'")


def _language_scenarios(
    application, config, settings, settings_app, settings_dialogs, wayland_capabilities
):
    """Switching language re-renders the window and every dialog follows.

    Returns what it built, because the right-to-left scenarios go on from here."""
    from wayland_vnc import i18n

    adw, gtk = settings_app._load_gtk()
    kit = settings_dialogs.Toolkit(adw, gtk)
    rtl_dir = gtk.TextDirection.RTL

    from gi.repository import GLib

    def settle(_window):
        """Run pending main-loop work so the presented tree is laid out and
        allocations are real, not zero."""
        for _ in range(50):
            if not GLib.MainContext.default().iteration(False):
                break

    def text_dir(widget):
        """The widget's text direction, asked of the Widget class on purpose:
        subclasses such as MenuButton redefine get_direction() to mean the arrow."""
        return gtk.Widget.get_direction(widget)

    def inside_spin(widget):
        """Numeric entries keep LTR digits under RTL, as GTK and the HIG require."""
        parent = widget.get_parent()
        while parent is not None:
            if type(parent).__name__ in ("SpinButton", "SpinRow"):
                return True
            parent = parent.get_parent()
        return False

    def ltr_leftovers(root):
        return sorted(
            {type(w).__name__ for w in walk(root) if text_dir(w) != rtl_dir and not inside_spin(w)}
        )

    def frames(root, count=6):
        """Let a few frames render: presentation animates, and a snapshot needs
        at least one drawn frame."""
        for _ in range(count):
            settle(root)
            GLib.usleep(40000)
        settle(root)

    def wait_laid_out(root, widgets, tries=40):
        """Iterate until every widget has a non-empty allocation AND its position
        has stopped moving -- dialogs slide in, and a mid-animation read is a
        coin toss."""
        previous = None
        for _ in range(tries):
            settle(root)
            bounds = [w.compute_bounds(root) for w in widgets]
            if bounds and all(ok and rect.get_width() > 0 for ok, rect in bounds):
                current = tuple(round(rect.get_x()) for _ok, rect in bounds)
                if current == previous:
                    return True
                previous = current
            GLib.usleep(40000)
        return False

    def automatic_label(_window):
        return i18n._("Automatic (match the desktop)")

    def choose_language(window, label, *, search=None):
        """Open the chooser from the Language row and activate the row titled `label`;
        with `search`, type it first and require the search to keep exactly that row
        (searching the way a person does: accents optional, case irrelevant)."""
        dialog = settings_dialogs.language_dialog(kit, lang_actions, window.relanguage)
        titles = {row.get_title(): row for row in dialog.rows.values()}
        check(label in titles, f"the language chooser offers {label!r}")
        if search is not None:
            dialog.search_entry.set_text(search)
            dialog.search_entry.emit("search-changed")
            matches = dialog.listbox.matches
            kept = [row.get_title() for row in dialog.rows.values() if matches(row)]
            check(kept == [label], f"searching {search!r} keeps only {label!r}, kept {kept}")
        titles[label].emit("activated")
        settle(window)

    def menu_labels(root):
        model = widgets_of(root, "MenuButton")[0].get_menu_model()
        return [
            model.get_item_attribute_value(i, "label", None).get_string()
            for i in range(model.get_n_items())
        ]

    def titles_in(root):
        return [w.get_title() for w in walk(root) if hasattr(w, "get_title") and w.get_title()]

    lang_actions = settings.Actions(
        config,
        generate_key=lambda path: path.write_text("demo-key", encoding="utf-8"),
        capabilities=wayland_capabilities,
    )
    i18n.activate(i18n.AUTOMATIC)
    gtk.Widget.set_default_direction(gtk.TextDirection.LTR)
    window = settings_app.build_window(application, lang_actions)
    window.present()
    settle(window)
    check("Status" in titles_in(window), "the window starts in English under an English locale")
    choose_language(window, "Română", search="ROMANA")  # no accents, wrong case: still found
    check(i18n.read_language(config) == "ro", "the choice is stored on disk")
    check("Stare" in titles_in(window), "the window re-rendered in Romanian in place")
    check(
        "Despre Wayland VNC" in menu_labels(window),
        f"the primary menu is relabelled in Romanian ({menu_labels(window)})",
    )
    check(os.environ.get("LANGUAGE") == "ro", "the toolkit's own strings are pointed at Romanian")
    credits_label = _gettext.dgettext("libadwaita", "Credits")
    if credits_label == "Credits":
        print(
            "  note: no Romanian libadwaita language pack on this platform; "
            "About's own labels stay English here"
        )
    else:
        check(
            credits_label != "Credits",
            f"libadwaita's About labels follow the choice ({credits_label})",
        )
    for key, expected in (
        ("credential", "Setează Parola Vizualizatorului".lower()),
        ("network", "Adresa și Portul de Ascultare".lower()),
        ("diagnostic", "Diagnosticare".lower()),
    ):
        opened = settings_dialogs.open_for(key, kit, lang_actions, window, lambda: None)
        dialog = opened.dialog if hasattr(opened, "dialog") else opened
        settle(window)
        check(
            dialog.get_title().lower() == expected,
            f"navigating to {key!r} shows a Romanian title",
        )
        dialog.force_close()
    about = settings_dialogs.about_dialog(kit, lang_actions)
    check(about.application_name == "Wayland VNC", "the product name is not translated")
    choose_language(window, automatic_label(window))
    check("Status" in titles_in(window), "Automatic returns the window to the desktop language")
    window.close()
    return SimpleNamespace(
        automatic_label=automatic_label,
        choose_language=choose_language,
        frames=frames,
        gtk=gtk,
        i18n=i18n,
        kit=kit,
        lang_actions=lang_actions,
        ltr_leftovers=ltr_leftovers,
        menu_labels=menu_labels,
        rtl_dir=rtl_dir,
        settle=settle,
        titles_in=titles_in,
        wait_laid_out=wait_laid_out,
    )


def _rtl_scenarios(application, config, settings_app, settings_dialogs, staged_lib, work, lang):
    """Arabic mirrors every UI element into a correct right-to-left layout."""
    automatic_label = lang.automatic_label
    choose_language = lang.choose_language
    frames = lang.frames
    gtk = lang.gtk
    i18n = lang.i18n
    kit = lang.kit
    lang_actions = lang.lang_actions
    ltr_leftovers = lang.ltr_leftovers
    menu_labels = lang.menu_labels
    rtl_dir = lang.rtl_dir
    settle = lang.settle
    titles_in = lang.titles_in
    wait_laid_out = lang.wait_laid_out
    window = settings_app.build_window(application, lang_actions)
    window.present()
    settle(window)
    choose_language(window, "العربية")
    check(i18n.read_language(config) == "ar", "Arabic is stored as the language")
    check(gtk.Widget.get_default_direction() == rtl_dir, "the toolkit default direction is RTL")
    # Every element, not a sample: anything still LTR would render its text and
    # controls the wrong way round inside a mirrored window.
    wrong = ltr_leftovers(window)
    check(not wrong, f"every widget in the window is RTL (LTR leftovers: {wrong})")
    width = window.get_width()
    menu = widgets_of(window, "MenuButton")[0]
    check(
        menu.compute_bounds(window)[1].get_x() < width / 2,
        "the primary menu button moved to the visual left of the header bar",
    )
    rows = widgets_of(window, "ActionRow")
    mirrored = 0
    for row in rows:
        title = next(
            (
                w
                for w in walk(row)
                if type(w).__name__ == "Label" and w.get_text() == row.get_title()
            ),
            None,
        )
        suffix = next((w for w in walk(row) if type(w).__name__ in ("Button", "Image")), None)
        if title is None or suffix is None:
            continue
        ok, sb = suffix.compute_bounds(window)
        ok2, tb = title.compute_bounds(window)
        if ok and ok2 and sb.get_x() < tb.get_x():
            mirrored += 1
    check(
        mirrored >= 3,
        f"row suffixes sit on the visual left of their titles ({mirrored} rows)",
    )
    # Text: no English msgid may leak through once Arabic is active.
    mo = staged_lib.parent.parent / "share" / "locale" / "ar" / "LC_MESSAGES" / "wayland-vnc.mo"
    with mo.open("rb") as handle:
        catalogue = _gettext.GNUTranslations(handle)._catalog
    english = {k for k in catalogue if k}
    leaked = sorted(t for t in [*titles_in(window), *menu_labels(window)] if t in english)
    check(not leaked, f"no untranslated English title is shown in Arabic ({leaked})")
    # Dialogs: header buttons mirror (Cancel to the visual right, Save to the left),
    # and every element inside is RTL too.
    for key in ("credential", "network"):
        opened = settings_dialogs.open_for(key, kit, lang_actions, window, lambda: None)
        settle(window)
        dialog = opened.dialog
        buttons = [w for w in walk(dialog) if type(w).__name__ == "Button" and w.get_label()]
        check(wait_laid_out(dialog, buttons), f"{key} dialog: header buttons were laid out")
        xs = {b.get_label(): b.compute_bounds(dialog)[1].get_x() for b in buttons}
        cancel = catalogue.get("Cancel")
        primary = catalogue.get("Save" if key == "credential" else "Apply")
        check(
            cancel in xs and primary in xs and xs[cancel] > xs[primary],
            f"{key} dialog: Cancel is on the visual right, the primary action on the left",
        )
        inner = ltr_leftovers(dialog)
        check(not inner, f"{key} dialog: every element is RTL (leftovers: {inner})")
        dialog.force_close()
    diag = settings_dialogs.open_for("diagnostic", kit, lang_actions, window, lambda: None)
    settle(window)
    check(not ltr_leftovers(diag), "diagnostics dialog is fully RTL")
    diag.force_close()
    # A picture for the record, next to the other run artifacts.
    # Rendered through the window's own renderer from a snapshot of its child --
    # the way GTK produces offscreen images -- so it works under Xvfb.
    frames(window)
    shot = work / "settings-arabic-rtl.png"
    why = "ok"
    # Rendering text with no font installed segfaults inside GDK's renderer (seen
    # on Tumbleweed): a glyph node with no Pango font behind it. Refuse to render
    # in that case and say so; a container without fonts is a container problem,
    # not an RTL one, and a crash would hide every verdict after it.
    import gi

    gi.require_version("PangoCairo", "1.0")
    from gi.repository import PangoCairo

    if not PangoCairo.FontMap.get_default().list_families():
        why = "skipped: no fonts installed on this platform"
    else:
        paintable = gtk.WidgetPaintable.new(window)
        width, height = paintable.get_intrinsic_width(), paintable.get_intrinsic_height()
        snapshot = gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is None:
            why = "no render node: the window has not drawn a frame"
        else:
            texture = window.get_renderer().render_texture(node, None)
            if texture is None:
                why = "the renderer produced no texture"
            elif not texture.save_to_png(str(shot)):
                why = "save_to_png failed"
    check(
        why == "ok" and shot.stat().st_size > 1000 or why.startswith("skipped"),
        f"an RTL screenshot was saved: {shot} ({why})",
    )
    choose_language(window, automatic_label(window))
    check(gtk.Widget.get_default_direction() != rtl_dir, "returning to Automatic restores LTR")
    window.close()


def main() -> int:
    staged_lib = Path(sys.argv[1])
    work = Path(sys.argv[2])
    sys.path.insert(0, str(staged_lib))
    fake_systemctl(work)

    from wayland_vnc import runtime, settings, settings_app, settings_dialogs

    print(f"== driving the staged app from {staged_lib} ==")
    check(
        Path(settings_app.__file__).resolve().is_relative_to(staged_lib.resolve()),
        "the app under test is the packaged copy, not the source tree",
    )

    config = work / "config"
    # Pin the capability verdict so the run is identical on a developer's Wayland
    # desktop and inside a container that has no session at all.
    wayland_capabilities = {
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
    actions = settings.Actions(
        config,
        generate_key=lambda path: path.write_text("demo-key", encoding="utf-8"),
        capabilities=wayland_capabilities,
    )
    # Some supported platforms ship no headless display server at all (RHEL 10 and
    # rebuilds have neither Xvfb nor a GTK broadway backend). There, the window cannot
    # be constructed by anyone, so the widget scenarios are reported as skipped rather
    # than silently passing; every display-independent scenario still runs.
    gui = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    application = None
    if gui:
        adw, _gtk = settings_app._load_gtk()
        application = adw.Application(application_id="io.github.ventura8.wayland_vnc.E2E")
        # Emit GApplication::startup before any window is added, as a real launch would.
        application.register(None)

    print("== happy: a fresh host reports nothing configured ==")
    status = actions.status()
    check(not status.credential.present, "no credential is reported on a fresh config dir")
    check(not status.config.present, "no server configuration is reported")
    rows = dict(settings_app.status_rows(status))
    check(rows[CREDENTIAL_ROW] == "Not set", "status row says the credential is not set")
    check(rows[CONFIG_ROW] == "Not provisioned", "status row says not provisioned")

    print("== bad: the service control is refused with a reason before setup ==")
    options = {option.key: option for option in actions.options(status)}
    # The service has no option row: its switches carry the reason instead.
    check("service" not in options, "no option row duplicates the Service switches")
    check(
        actions.service_reason(status) == "Set a viewer password first",
        "the Service group states the missing precondition",
    )
    check(not options["network"].available, "network option is gated on the credential")

    print("== bad: a weak password is refused and writes nothing ==")
    try:
        actions.set_credential("vnc", "short")
        check(False, "weak password should have been refused")
    except ValueError:
        check(True, "weak password refused")
    check(
        not (config / "credentials").exists(),
        "no credential file is left behind by the refused attempt",
    )

    print("== happy: setting the credential writes mode 600 ==")
    path = actions.set_credential("vnc", "hunter2x")
    check(path.exists(), "credential file created")
    check(format(path.stat().st_mode & 0o777, "03o") == "600", "credential file is mode 600")

    print("== bad: a privileged port is refused and leaves no config ==")
    try:
        actions.apply_network("127.0.0.1", 443)
        check(False, "privileged port should have been refused")
    except ValueError:
        check(True, "privileged port refused")
    check(
        not (config / "wayvnc.conf").exists(),
        "no server configuration is written by the refused attempt",
    )

    print("== happy: provisioning writes an authenticated, 0600 config ==")
    config_path = actions.apply_network("127.0.0.1", 5900)
    body = config_path.read_text(encoding="utf-8")
    check("address=127.0.0.1" in body, "an explicit loopback address is honoured")
    check("enable_auth=true" in body, "provisioned config enables auth")
    check(
        format(config_path.stat().st_mode & 0o777, "03o") == "600",
        "server configuration is mode 600",
    )
    status = actions.status()
    check(status.config.scope == "loopback", "an explicit 127.0.0.1 bind reads as loopback")
    check(status.credential.secure_mode, "status reports the credential mode as secure")

    # Only the widget scenarios need a display. The checks after them are
    # display-independent and run everywhere; skipping them along with the widgets
    # once let a platform without Xvfb pass with a third of the suite unrun.
    if application is None:
        print("== SKIPPED: window and switch scenarios (no headless display on this platform) ==")
    else:
        _window_scenarios(
            actions=actions,
            adw=adw,
            application=application,
            settings=settings,
            settings_app=settings_app,
            wayland_capabilities=wayland_capabilities,
            work=work,
            _gtk=_gtk,
        )

    print("== bad: a failing systemctl surfaces the error instead of lying ==")
    failing = settings.Actions(
        config,
        run=lambda args: subprocess.CompletedProcess(args, 1, "", "unit refused to start"),
        generate_key=lambda path: None,
        capabilities=wayland_capabilities,
    )
    try:
        failing.set_active(True)
        check(False, "a failing systemctl should raise")
    except RuntimeError as error:
        check("unit refused to start" in str(error), "systemd's stderr is surfaced verbatim")

    print("== bad: a loose credential mode is reported, not hidden ==")
    (config / "credentials").chmod(0o644)
    loose = dict(settings_app.status_rows(actions.status()))
    check("too open" in loose[CREDENTIAL_ROW], "a 0644 credential is flagged as too open")
    (config / "credentials").chmod(0o600)

    print("== happy: the default bind is this machine only; the switch opens the LAN ==")
    actions.apply_network(runtime.DEFAULT_ADDRESS, 5900)
    default = dict(settings_app.status_rows(actions.status()))
    check(
        "this machine only" in default[CONFIG_ROW],
        "the default 127.0.0.1 bind is described as this machine only",
    )
    check(not actions.status().config.lan_access, "local network access starts off")
    actions.set_lan_access(True)
    lan = dict(settings_app.status_rows(actions.status()))
    check(
        "every network this computer is on" in lan[CONFIG_ROW],
        "the opt-in is described as every network this computer is on, no fence claimed",
    )
    actions.set_lan_access(False)
    check(actions.status().config.loopback_only, "the switch goes back to this machine only")
    print("== bad: a public address is shouted about ==")
    actions.apply_network("8.8.8.8", 5900)
    exposed = dict(settings_app.status_rows(actions.status()))
    check(
        "PUBLIC ADDRESS" in exposed[CONFIG_ROW],
        "a public bind is called out loudly in the status view",
    )

    print("== bad: a non-Wayland session disables the service control with the reason ==")
    x11 = settings.Actions(
        config,
        generate_key=lambda path: None,
        capabilities={
            "session_type": "x11",
            "interfaces": [],
            "gnome_remote_desktop": False,
            "gnome_screencast": False,
            "kwin": False,
            "remote_desktop_portal": False,
            "screencast_portal": False,
            "binaries": {},
        },
    )
    reason = x11.service_reason(x11.status())
    check(reason != "", "service control is disabled under X11")
    check("Wayland" in reason, "the X11 refusal reason is shown")

    print("== happy: switching language re-renders the window, and every dialog follows ==")
    if application is None:
        print("  skipped: no display server on this platform")
    else:
        lang = _language_scenarios(
            application=application,
            config=config,
            settings=settings,
            settings_app=settings_app,
            settings_dialogs=settings_dialogs,
            wayland_capabilities=wayland_capabilities,
        )

    print("== happy: Arabic mirrors every UI element into a correct right-to-left layout ==")
    if application is None:
        print("  skipped: no display server on this platform")
    else:
        _rtl_scenarios(
            application=application,
            config=config,
            settings_app=settings_app,
            settings_dialogs=settings_dialogs,
            staged_lib=staged_lib,
            work=work,
            lang=lang,
        )

    print()
    if FAILURES:
        print(f"SETTINGS APP E2E FAILED ({len(FAILURES)} check(s))")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    if application is None:
        print("SETTINGS APP E2E PASSED (display-independent scenarios only)")
    else:
        print("SETTINGS APP E2E PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
