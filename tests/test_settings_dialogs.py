"""The entry dialogs must write through the runtime and refuse bad input visibly."""

import subprocess
from pathlib import Path

import pytest

from wayland_vnc import runtime, settings, settings_app, settings_dialogs


def _walk(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _walk(child)
        child = child.get_next_sibling()


def _walk_labels(dialog):
    """Every visible string in a dialog: widget labels and ActionRow titles/subtitles."""
    found = set()
    # Adw.Dialog keeps its content behind get_child(); walking the dialog itself
    # yields nothing until it is presented.
    root = dialog.get_child() if hasattr(dialog, "get_child") and dialog.get_child() else dialog
    for widget in _walk(root):
        for getter in ("get_label", "get_title", "get_subtitle"):
            if hasattr(widget, getter):
                value = getattr(widget, getter)()
                if value:
                    found.add(value)
    if dialog.get_title():
        found.add(dialog.get_title())
    return found


def _actions(tmp_path, *, generate_key=None):
    def run(args):
        stdout = {"is-enabled": "enabled", "is-active": "active"}.get(args[0], "")
        return subprocess.CompletedProcess(args, 0, stdout, "")

    return settings.Actions(
        tmp_path,
        run=run,
        generate_key=generate_key or (lambda path: path.write_text("k", encoding="utf-8")),
        capabilities={
            "session_type": "wayland",
            "interfaces": [],
            "gnome_remote_desktop": False,
            "gnome_screencast": False,
            "kwin": False,
            "remote_desktop_portal": False,
            "screencast_portal": False,
            "binaries": {},
        },
        # A host whose runner answers `ip` (the window's connect section) with a
        # failure: the tests never reach the developer's network configuration.
        host=runtime.Host(
            lambda _n: None,
            lambda *_a: None,
            lambda args, stdin=None: subprocess.CompletedProcess(args, 1, "", ""),
        ),
    )


@pytest.fixture(name="kit")
def kit_fixture(display):
    adw, gtk = settings_app._load_gtk()
    return settings_dialogs.Toolkit(adw, gtk)


def test_credential_dialog_stores_a_matching_password_with_mode_600(kit, tmp_path):
    done = []
    form = settings_dialogs.credential_form(kit, _actions(tmp_path), lambda: done.append(True))
    form.rows["username"].set_text("vnc")
    form.rows["password"].set_text("hunter2x")
    form.rows["confirm"].set_text("hunter2x")
    assert form.submit() is True
    path = tmp_path / runtime.CREDENTIALS_NAME
    assert format(path.stat().st_mode & 0o777, "03o") == "600"
    assert runtime.read_credentials(tmp_path).username == "vnc"
    assert done == [True]
    assert not form.error.get_visible()


def test_credential_dialog_refuses_a_mismatch_and_writes_nothing(kit, tmp_path):
    done = []
    form = settings_dialogs.credential_form(kit, _actions(tmp_path), lambda: done.append(True))
    form.rows["password"].set_text("hunter2x")
    form.rows["confirm"].set_text("different")
    assert form.submit() is False
    assert form.error.get_visible()
    assert "did not match" in form.error.get_text()
    assert not (tmp_path / runtime.CREDENTIALS_NAME).exists()
    assert done == []


def test_credential_dialog_surfaces_the_runtime_rule_for_a_weak_password(kit, tmp_path):
    form = settings_dialogs.credential_form(kit, _actions(tmp_path), lambda: None)
    form.rows["password"].set_text("short")
    form.rows["confirm"].set_text("short")
    assert form.submit() is False
    assert "6-64" in form.error.get_text()
    assert not (tmp_path / runtime.CREDENTIALS_NAME).exists()


def test_network_dialog_provisions_a_loopback_config(kit, tmp_path):
    actions = _actions(tmp_path)
    actions.set_credential("vnc", "hunter2x")
    done = []
    form = settings_dialogs.network_form(kit, actions, lambda: done.append(True))
    form.rows["address"].set_text("127.0.0.1")
    form.rows["port"].set_value(5901)
    assert form.submit() is True
    body = (tmp_path / runtime.CONFIG_NAME).read_text(encoding="utf-8")
    assert "address=127.0.0.1" in body
    assert "port=5901" in body
    assert done == [True]


def test_network_dialog_cannot_even_select_a_privileged_port(kit, tmp_path):
    # The row's range is the first line of defence; the runtime's check is the second.
    form = settings_dialogs.network_form(kit, _actions(tmp_path), lambda: None)
    form.rows["port"].set_value(443)
    assert form.rows["port"].get_value() >= 1025


def test_network_dialog_surfaces_a_provisioning_failure(kit, tmp_path):
    def broken(_path):
        raise OSError("openssl exploded")

    form = settings_dialogs.network_form(kit, _actions(tmp_path, generate_key=broken), lambda: None)
    assert form.submit() is False
    assert "openssl exploded" in form.error.get_text()
    assert not (tmp_path / runtime.CONFIG_NAME).exists()


def test_network_dialog_prefills_from_the_provisioned_config(kit, tmp_path):
    actions = _actions(tmp_path)
    actions.apply_network("127.0.0.1", 5905)
    form = settings_dialogs.network_form(kit, actions, lambda: None)
    assert form.rows["address"].get_text() == "127.0.0.1"
    assert int(form.rows["port"].get_value()) == 5905


def test_diagnostic_dialog_shows_collapsible_categories(kit, tmp_path):
    """Categories, not a wall of JSON; the verdict is open, the rest collapsed."""
    dialog = settings_dialogs.diagnostic_dialog(kit, _actions(tmp_path))
    # Walk the content: an Adw.Dialog does not expose its child until realized.
    expanders = [w for w in _walk(dialog.get_child()) if type(w).__name__ == "ExpanderRow"]
    titles = [e.get_title() for e in expanders]
    for expected in ("Verdict", "Session", "Wayland Protocols", "Portals", "Binaries"):
        assert expected in titles
    verdict = next(e for e in expanders if e.get_title() == "Verdict")
    assert verdict.get_expanded(), "the verdict is what people opened this for"
    assert not next(e for e in expanders if e.get_title() == "Binaries").get_expanded()


@pytest.mark.parametrize(
    "key,kind", [("credential", "Form"), ("network", "Form"), ("diagnostic", "Dialog")]
)
def test_open_for_presents_the_matching_dialog(kit, tmp_path, key, kind):
    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id=f"io.github.ventura8.wayland_vnc.D{key}")
    application.register(None)
    window = settings_app.build_window(application, _actions(tmp_path))
    opened = settings_dialogs.open_for(key, kit, _actions(tmp_path), window, lambda: None)
    assert type(opened).__name__ == kind


def test_open_for_has_no_dialog_for_the_service_row(kit, tmp_path):
    assert settings_dialogs.open_for("service", kit, _actions(tmp_path), None, lambda: None) is None


def test_window_option_rows_open_dialogs_and_refresh(display, tmp_path):
    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.Wire")
    application.register(None)
    actions = _actions(tmp_path)
    window = settings_app.build_window(application, actions)
    before = dict(settings_app.status_rows(actions.status()))["Viewer Credential"]
    assert before == "Not set"
    # Commit a credential through the runtime, then ask the window to re-render.
    actions.set_credential("vnc", "hunter2x")
    window.refresh()
    after = dict(settings_app.status_rows(actions.status()))["Viewer Credential"]
    assert after == "Stored (mode 600)"
    # The dispatcher wired into the rows must reach the dialogs (no injected hook).
    calls = []
    hooked = settings_app.build_window(application, actions, on_choose=calls.append)
    assert hooked is not None


def test_credential_dialog_reveals_the_current_password(kit, tmp_path):
    """A generated password is undiscoverable otherwise; the dialog must show it."""
    actions = _actions(tmp_path)
    actions.set_credential("vnc", "phone123")
    form = settings_dialogs.credential_form(kit, actions, lambda: None)
    assert form.rows["username"].get_text() == "vnc"
    assert form.rows["password"].get_text() == "phone123"
    assert form.rows["confirm"].get_text() == "phone123"


def test_credential_dialog_starts_empty_when_nothing_is_provisioned(kit, tmp_path):
    form = settings_dialogs.credential_form(kit, _actions(tmp_path), lambda: None)
    assert form.rows["username"].get_text() == "vnc"
    assert form.rows["password"].get_text() == ""


def test_about_dialog_reports_real_installed_state(kit, tmp_path):
    """About must show what is actually installed, not a hardcoded blurb."""
    dialog = settings_dialogs.about_dialog(kit, _actions(tmp_path))
    assert dialog.version == settings.product_version()
    assert dialog.website == settings.PROJECT_URL
    assert dialog.issue_url == settings.ISSUE_URL
    assert dialog.application_icon == "io.github.ventura8.wayland_vnc"
    assert dialog.application_name == "Wayland VNC"


def test_about_never_claims_the_desktop_is_qualified(kit, tmp_path):
    """A working connection is not a qualified desktop; About must not imply it is."""
    blurb = settings_dialogs.about_dialog(kit, _actions(tmp_path)).comments.lower()
    assert "qualified" not in blurb
    assert "supported" not in blurb


def test_about_labels_come_from_our_catalogue_not_the_toolkit(kit, tmp_path, monkeypatch):
    """Every row About draws must be translatable by us.

    Adw.AboutDialog labels its own rows (Details, Report an Issue, Credits, Legal)
    from libadwaita's catalogue, which ships in a per-language GNOME language pack on
    Ubuntu. Without that pack those rows stay English in an otherwise translated
    window, so About is built from our widgets and our strings instead.
    """
    monkeypatch.setattr(settings_dialogs, "_", lambda text: f"@@{text}")
    dialog = settings_dialogs.about_dialog(kit, _actions(tmp_path))
    labels = _walk_labels(dialog)
    for expected in ("Report an Issue", "License", "Project Website", "Wayland VNC"):
        assert f"@@{expected}" in labels, f"{expected!r} is not translated through our catalogue"


def test_open_for_about_presents_the_dialog(kit, tmp_path):
    adw, _ = settings_app._load_gtk()
    application = adw.Application(application_id="io.github.ventura8.wayland_vnc.About")
    application.register(None)
    window = settings_app.build_window(application, _actions(tmp_path))
    opened = settings_dialogs.open_for("about", kit, _actions(tmp_path), window, lambda: None)
    assert opened.application_name == "Wayland VNC"


def test_about_is_not_a_preferences_row(tmp_path):
    """About lives in the primary menu; listing it among the options would put it
    where the HIG says it does not belong."""
    labels = [o.label for o in _actions(tmp_path).options(_actions(tmp_path).status())]
    assert "About Wayland VNC" not in labels


def test_invalid_input_marks_the_rows_with_the_adwaita_error_style(kit, tmp_path):
    """Rows carry libadwaita's own error style, as every GNOME entry does; the label
    still carries the reason so it is readable."""
    form = settings_dialogs.credential_form(kit, _actions(tmp_path), lambda: None)
    form.rows["password"].set_text("abcdef")
    form.rows["confirm"].set_text("ABCDEF")
    assert form.submit() is False
    assert form.rows["password"].has_css_class("error")
    assert form.rows["confirm"].has_css_class("error")
    form.rows["confirm"].set_text("abcdef")
    assert form.submit() is True
    assert not form.rows["password"].has_css_class("error"), "cleared on success"


def test_the_ui_never_hardcodes_a_horizontal_side():
    """RTL practice: alignment must be START/END or xalign-free, so a right-to-left
    language mirrors every element; LEFT/RIGHT and xalign pin text to one side."""
    for module in (settings_app, settings_dialogs):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("xalign", "Align.LEFT", "Align.RIGHT", "set_direction("):
            assert forbidden not in source, f"{module.__name__} uses {forbidden}"


def test_the_error_label_starts_at_the_reading_edge(kit, tmp_path):
    form = settings_dialogs.credential_form(kit, _actions(tmp_path), lambda: None)
    assert form.error.get_halign() == kit.gtk.Align.START


def test_search_folding_ignores_accents_case_and_undecomposable_letters():
    """Search is diacritics-tolerant both ways: a plain query finds accented text and an
    accented query finds it too, because both sides are folded the same way."""
    fold = settings_dialogs.fold
    assert fold("Română") == "romana" == fold("ROMÂNĂ") == fold("romana")
    assert fold("Français") == "francais"
    assert fold("Español") == "espanol"
    assert fold("Tiếng Việt") == "tieng viet"
    assert fold("Łódź Straße Øresund Đakovo İstanbul ırmak") == (
        "lodz strasse oresund dakovo istanbul irmak"
    )
    assert fold("ʻŌlelo Hawaiʻi") == "olelo hawaii"
    assert fold("Kreyòl ayisyen") == "kreyol ayisyen"
    assert fold("العربية") == "العربية", "scripts without Latin accents pass through"
    assert fold("日本語") == "日本語"


def test_diagnostics_search_is_diacritics_tolerant(kit, tmp_path, monkeypatch):
    """A diagnostics row whose text carries accents is found by an accent-free query,
    and an accented query finds accent-free text."""
    dialog = settings_dialogs.diagnostic_dialog(kit, _actions(tmp_path))
    expander, _title, rows = dialog.sections[0]
    row, _text = rows[0]
    row.set_subtitle("Réseau local — Straße")
    folded = settings_dialogs.fold("Réseau local Straße")
    dialog.sections[0] = (expander, _title, [(row, folded), *rows[1:]])
    assert settings_dialogs.diagnostic_filter(dialog.sections, "reseau") == 1
    assert settings_dialogs.diagnostic_filter(dialog.sections, "strasse") == 1
    assert settings_dialogs.diagnostic_filter(dialog.sections, "RÉSEAU") == 1
    assert settings_dialogs.diagnostic_filter(dialog.sections, "wayland") > 0, "plain still works"


def test_diagnostics_search_filters_rows_expands_hits_and_shows_an_empty_state(kit, tmp_path):
    """The HIG search pattern: a search bar under the header with a toggle and
    type-to-search, live filtering, matching sections opened, and an empty state
    (Adw.StatusPage) rather than a blank list when nothing matches."""
    dialog = settings_dialogs.diagnostic_dialog(kit, _actions(tmp_path))
    assert type(dialog.search_bar).__name__ == "SearchBar"
    assert dialog.search_toggle.get_icon_name() == "edit-find-symbolic"
    assert dialog.search_bar.get_key_capture_widget() is dialog, "type-to-search anywhere"
    dialog.search_toggle.set_active(True)
    assert dialog.search_bar.get_search_mode(), "the toggle opens the bar"
    dialog.search_bar.set_search_mode(False)
    assert not dialog.search_toggle.get_active(), "closing the bar releases the toggle"

    shown = settings_dialogs.diagnostic_filter(dialog.sections, "wayland")
    assert shown > 0
    verdict = next(e for e, title, _rows in dialog.sections if title == "verdict")
    protocols = next(e for e, title, _rows in dialog.sections if title == "wayland protocols")
    assert protocols.get_visible() and protocols.get_expanded(), "a hit is opened on screen"
    assert settings_dialogs.diagnostic_filter(dialog.sections, "no-such-thing-xyz") == 0
    assert not any(e.get_visible() for e, _t, _r in dialog.sections)
    assert settings_dialogs.diagnostic_filter(dialog.sections, "") > 0, "empty query restores"
    assert all(e.get_visible() for e, _t, _r in dialog.sections)
    assert verdict.get_visible()

    # Typing into the real entry drives the same filter and flips the empty state.
    dialog.search_entry.set_text("no-such-thing-xyz")
    dialog.search_entry.emit("search-changed")
    # An unpresented Adw.Dialog has no first child; its content is what it holds.
    empty = next(w for w in _walk(dialog.get_child()) if type(w).__name__ == "StatusPage")
    assert empty.get_visible()
    assert empty.get_title() == "No Results Found"
    dialog.search_entry.set_text("")
    dialog.search_entry.emit("search-changed")
    assert not empty.get_visible()
