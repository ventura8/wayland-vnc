"""libadwaita entry dialogs for the settings app: credential, network, diagnostics.

Every dialog writes through `settings.Actions`, so it inherits the runtime's own
validation and mode-600 file handling; nothing here re-implements provisioning. Each
form exposes the `submit` callable its Save button triggers, so tests build the widgets
headlessly, fill the rows, and invoke the very same handler -- error path included --
without simulating clicks.
"""

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from wayland_vnc import i18n, runtime
from wayland_vnc.i18n import _
from wayland_vnc.settings import (
    ISSUE_URL,
    PROJECT_URL,
    Actions,
    diagnostic_sections,
    product_version,
)


@dataclass(frozen=True)
class Toolkit:
    """The bound GTK/libadwaita modules, passed around instead of re-importing."""

    adw: Any
    gtk: Any


@dataclass
class Form:
    """A dialog plus the handler behind its primary button."""

    dialog: Any
    submit: Callable[[], bool]
    rows: dict = field(default_factory=dict)
    error: Any = None


def _error_row(kit: Toolkit):
    # halign START, never a fixed side: START mirrors to the right edge under a
    # right-to-left language, which is what the HIG's RTL guidance requires.
    label = kit.gtk.Label(wrap=True, halign=kit.gtk.Align.START, visible=False)
    label.add_css_class("error")
    return label


def _show_error(label, message: str, *rows) -> None:
    """Show the message, and mark the rows it concerns with libadwaita's error style.

    The style is what every GNOME entry uses for invalid input; the label carries the
    text so the reason is readable and accessible, not just a red outline.
    """
    label.set_text(message)
    label.set_visible(True)
    for row in rows:
        row.add_css_class("error")


def _clear_error(label, *rows) -> None:
    label.set_visible(False)
    for row in rows:
        row.remove_css_class("error")


def _close(dialog) -> None:
    """Close only a presented dialog; a form may be submitted before it is shown."""
    if dialog is not None and dialog.get_root() is not None:
        dialog.close()


def _dialog(kit: Toolkit, title: str, page, button_label: str, on_submit: Callable):
    """A standard dialog: header with Cancel/primary buttons above a preferences page."""
    dialog = kit.adw.Dialog(title=title, content_width=440)
    header = kit.adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
    cancel = kit.gtk.Button(label=_("Cancel"))
    cancel.connect("clicked", lambda _b: dialog.close())
    header.pack_start(cancel)
    primary = kit.gtk.Button(label=button_label)
    primary.add_css_class("suggested-action")
    primary.connect("clicked", lambda _b: on_submit())
    header.pack_end(primary)
    view = kit.adw.ToolbarView(content=page)
    view.add_top_bar(header)
    dialog.set_child(view)
    return dialog


def credential_form(kit: Toolkit, actions: Actions, on_done: Callable[[], None]) -> Form:
    """Username plus a password entered twice; stored via runtime.set_password (0600).

    The current credential is prefilled and revealable. The service generates a random
    password on first start, so without this there is no way to discover what to type
    on the phone -- and no way to check what is set before changing it.
    """
    current = actions.credential()
    page = kit.adw.PreferencesPage()
    group = kit.adw.PreferencesGroup(
        title=_("Viewer Credential"),
        description=_(
            "Type these into RealVNC Viewer on the other device. Use the eye to reveal "
            "the current password. Stored with mode 600."
        ),
    )
    username = kit.adw.EntryRow(title=_("Username"), text=current.username if current else "vnc")
    password = kit.adw.PasswordEntryRow(title=_("Password"))
    confirm = kit.adw.PasswordEntryRow(title=_("Confirm Password"))
    if current is not None:
        password.set_text(current.password)
        confirm.set_text(current.password)
    error = _error_row(kit)
    for row in (username, password, confirm):
        group.add(row)
    page.add(group)
    note = kit.adw.PreferencesGroup()
    note.add(error)
    page.add(note)
    form = Form(
        dialog=None,
        submit=lambda: False,
        rows={
            "username": username,
            "password": password,
            "confirm": confirm,
        },
        error=error,
    )

    def submit() -> bool:
        _clear_error(error, username, password, confirm)
        if password.get_text() != confirm.get_text():
            _show_error(error, _("Passwords did not match"), password, confirm)
            return False
        try:
            actions.set_credential(username.get_text(), password.get_text())
        except (ValueError, RuntimeError, OSError) as failure:
            _show_error(error, _(str(failure)), password, confirm)
            return False
        _close(form.dialog)
        on_done()
        return True

    form.submit = submit
    form.dialog = _dialog(kit, _("Set Viewer Password"), page, _("Save"), submit)
    return form


def network_form(kit: Toolkit, actions: Actions, on_done: Callable[[], None]) -> Form:
    """Bind address and port; provisioned via runtime.provision with its validation."""
    current = actions.status().config
    page = kit.adw.PreferencesPage()
    group = kit.adw.PreferencesGroup(
        title=_("Bind Address and Port"),
        description=_(
            "127.0.0.1: this computer only. 0.0.0.0: every network this computer is "
            "on, with no other fence. Ports at or below 1024 are refused."
        ),
    )
    address = kit.adw.EntryRow(title=_("Address"), text=current.address or runtime.DEFAULT_ADDRESS)
    port = kit.adw.SpinRow.new_with_range(1025, 65535, 1)
    port.set_title(_("Port"))
    port.set_value(current.port or runtime.DEFAULT_PORT)
    error = _error_row(kit)
    group.add(address)
    group.add(port)
    page.add(group)
    note = kit.adw.PreferencesGroup()
    note.add(error)
    page.add(note)
    form = Form(
        dialog=None, submit=lambda: False, rows={"address": address, "port": port}, error=error
    )

    def submit() -> bool:
        _clear_error(error, address, port)
        try:
            actions.apply_network(address.get_text().strip(), int(port.get_value()))
        except (ValueError, OSError) as failure:
            _show_error(error, _(str(failure)), address, port)
            return False
        _close(form.dialog)
        on_done()
        return True

    form.submit = submit
    form.dialog = _dialog(kit, _("Bind Address and Port"), page, _("Apply"), submit)
    return form


def diagnostic_dialog(kit: Toolkit, actions: Actions):
    """The diagnostic report as collapsible categories of native rows.

    The raw report is a flat JSON blob. Each category is an `Adw.ExpanderRow` carrying
    a one-line summary, collapsed by default except the verdict, so the reader opens
    only what they care about instead of scrolling a wall of JSON.
    """
    status = actions.status()
    page = kit.adw.PreferencesPage()
    group = kit.adw.PreferencesGroup(
        title=_("Diagnostics"),
        description=_("What this desktop advertises, and what it means for remote access."),
    )
    sections = []  # (expander, [(row, searchable text)]) for the live filter
    for section in diagnostic_sections(status.diagnostic):
        expander = kit.adw.ExpanderRow(title=section.title, subtitle=section.summary)
        # The verdict is the answer most people opened this for.
        expander.set_expanded(section.key == "verdict")
        rows = []
        for name, value in section.rows:
            row = kit.adw.ActionRow(title=name, subtitle=value, subtitle_selectable=True)
            expander.add_row(row)
            rows.append((row, fold(f"{name} {value}")))
        group.add(expander)
        sections.append((expander, fold(section.title), rows))
    page.add(group)
    # Empty state for a query that matches nothing (Adw.StatusPage, the HIG's pattern),
    # rather than a silently blank list.
    empty = kit.adw.StatusPage(
        icon_name="edit-find-symbolic",
        title=_("No Results Found"),
        description=_("Try a different search."),
        visible=False,
    )
    stack = kit.gtk.Box(orientation=kit.gtk.Orientation.VERTICAL)
    stack.append(page)
    stack.append(empty)
    dialog = _dialog_with_close(kit, _("Diagnostics"), stack)
    page.set_vexpand(True)
    empty.set_vexpand(True)
    _searchable(kit, dialog, sections, page=page, empty=empty)
    return dialog


# Letters whose accent-free form is not a Unicode decomposition, so NFKD leaves them
# alone: a search for "lodz" or "strasse" should still find "Łódź" and "Straße".
_PLAIN_LETTERS = str.maketrans(
    {
        "ø": "o",
        "ł": "l",
        "đ": "d",
        "ð": "d",
        "þ": "th",
        "ı": "i",
        "ħ": "h",
        "ŧ": "t",
        "æ": "ae",
        "œ": "oe",
        "ĸ": "k",
        "ŋ": "n",
        "ɩ": "i",
        "ʻ": "",
        "ʼ": "",
        "'": "",
    }
)


def fold(text: str) -> str:
    """Reduce `text` to a form where accents and case no longer matter.

    Search must be diacritics-tolerant in both directions: "romana" finds "Română",
    and "Română" typed with the accents finds it too, because both sides go through
    this. Compatibility decomposition (NFKD) splits a letter from its combining marks,
    which are then dropped; the handful of letters that do not decompose are mapped
    by hand; casefold handles case and ligatures such as ß.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.translate(_PLAIN_LETTERS)


def _filter_section(expander, title: str, rows, needle: str) -> int:
    """Apply one folded query to one diagnostics section; returns how many of its rows
    are left showing. A section whose own title matches shows all of its rows."""
    if needle and needle in title:
        for row, _text in rows:
            row.set_visible(True)
        hits = len(rows)
    else:
        hits = 0
        for row, text in rows:
            visible = not needle or needle in text
            row.set_visible(visible)
            hits += visible
    expander.set_visible(not needle or hits > 0)
    if needle and hits:
        expander.set_expanded(True)
    return hits if expander.get_visible() else 0


def diagnostic_filter(sections, query: str) -> int:
    """Apply a search query to the diagnostics rows; returns how many rows match.

    A section stays visible if its own title matches or any of its rows do; matching
    sections are expanded so the hit is on screen. An empty query restores everything.
    """
    needle = fold(query.strip())
    return sum(_filter_section(expander, title, rows, needle) for expander, title, rows in sections)


def _searchable(kit: Toolkit, dialog, sections, *, page, empty) -> None:
    """The HIG search pattern: a search bar under the header, a toggle button in the
    header, Ctrl+F, and type-to-search anywhere in the dialog."""
    entry = kit.gtk.SearchEntry(placeholder_text=_("Search diagnostics"))
    search_bar = kit.gtk.SearchBar(child=entry, show_close_button=True)
    search_bar.connect_entry(entry)
    search_bar.set_key_capture_widget(dialog)
    view = dialog.get_child()
    view.add_top_bar(search_bar)
    toggle = kit.gtk.ToggleButton(icon_name="edit-find-symbolic")
    toggle.set_tooltip_text(_("Search"))
    # Two-way: the button opens the bar, and the bar's own close (or Escape) releases
    # the button, so the two never disagree.
    toggle.connect("toggled", lambda b: search_bar.set_search_mode(b.get_active()))
    search_bar.connect(
        "notify::search-mode-enabled", lambda b, _p: toggle.set_active(b.get_search_mode())
    )
    dialog.header.pack_end(toggle)

    def on_changed(widget):
        shown = diagnostic_filter(sections, widget.get_text())
        nothing = bool(widget.get_text().strip()) and shown == 0
        page.set_visible(not nothing)
        empty.set_visible(nothing)

    entry.connect("search-changed", on_changed)
    dialog.search_entry = entry
    dialog.search_bar = search_bar
    dialog.search_toggle = toggle
    dialog.sections = sections


def _about_header(kit: Toolkit, comments: str):
    """Icon, product name, author, version and the one-line description.

    The product name, the author and the version are identifiers rather than prose,
    so only the description is translated here.
    """
    body = kit.gtk.Box(orientation=kit.gtk.Orientation.VERTICAL, spacing=12)
    for margin in ("top", "bottom", "start", "end"):
        getattr(body, f"set_margin_{margin}")(24)
    icon = kit.gtk.Image(icon_name="io.github.ventura8.wayland_vnc", pixel_size=96)
    icon.add_css_class("icon-dropshadow")
    body.append(icon)
    for text, classes in (
        (_("Wayland VNC"), ("title-1",)),
        ("ventura8", ("dim-label",)),
        (product_version(), ("accent", "caption")),
    ):
        label = kit.gtk.Label(label=text)
        for css in classes:
            label.add_css_class(css)
        body.append(label)
    body.append(kit.gtk.Label(label=comments, wrap=True, justify=kit.gtk.Justification.CENTER))
    return body


def _about_link_row(kit: Toolkit, title: str, uri: str):
    """An activatable row that opens `uri`, labelled from our own catalogue."""
    row = kit.adw.ActionRow(title=title, subtitle=uri, activatable=True)
    row.add_suffix(kit.gtk.Image(icon_name="adw-external-link-symbolic"))
    row.uri = uri

    def open_link(_row):
        kit.gtk.UriLauncher(uri=uri).launch(None, None, None, None)

    row.connect("activated", open_link)
    return row


def about_dialog(kit: Toolkit, actions: Actions):
    """The desktop's own About presentation, filled from real installed state.

    Built from our own widgets rather than Adw.AboutDialog. That dialog looks native,
    but every label it draws itself -- Details, Report an Issue, Credits, Legal -- is
    translated by libadwaita's own catalogue, which on Ubuntu ships in a per-language
    GNOME language pack. On a machine without that pack those rows stay English while
    the rest of this window is translated, which is exactly what a user sees as a
    half-translated app, and no amount of work in `po/` can change it. Everything here
    is drawn with Adwaita widgets and labelled from our 101 catalogues instead, so
    About reads in the chosen language on any system, with nothing extra installed.

    The version is the one the installed package shipped, and the backend line is the
    verdict the probe reached on this desktop -- not a claim of support, which only
    qualification evidence gives.
    """
    status = actions.status()
    comments = _(
        "Makes this Wayland desktop reachable from RealVNC Viewer on your phone or "
        "another computer. It listens on this computer only until you turn on Local "
        "Network Access."
    )
    dialog = kit.adw.Dialog(title=_("About Wayland VNC"), content_width=440)
    view = kit.adw.ToolbarView()
    view.add_top_bar(kit.adw.HeaderBar())

    body = _about_header(kit, comments)

    state = kit.adw.PreferencesGroup()
    state.add(
        kit.adw.ActionRow(
            title=_("Backend on This Desktop"),
            subtitle=status.backend_candidate or _("None detected"),
        )
    )
    body.append(state)

    links = kit.adw.PreferencesGroup()
    links.add(_about_link_row(kit, _("Project Website"), PROJECT_URL))
    links.add(_about_link_row(kit, _("Report an Issue"), ISSUE_URL))
    links.add(kit.adw.ActionRow(title=_("License"), subtitle="GPL-2.0-or-later"))
    body.append(links)

    scroller = kit.gtk.ScrolledWindow(propagate_natural_height=True)
    scroller.set_child(body)
    view.set_content(scroller)
    dialog.set_child(view)

    # The facts About presents, exposed for the tests and the e2e without reaching
    # into the widget tree for them.
    dialog.application_name = _("Wayland VNC")
    dialog.application_icon = "io.github.ventura8.wayland_vnc"
    dialog.version = product_version()
    dialog.comments = comments
    dialog.website = PROJECT_URL
    dialog.issue_url = ISSUE_URL
    dialog.license = "GPL-2.0-or-later"
    dialog.developer_name = "ventura8"
    return dialog


def _language_rows(kit: Toolkit, current: str, on_activate: Callable):
    """The chooser's list: Automatic first, then every installed catalogue by endonym,
    the current choice marked with a check. Returns (listbox, {code: row})."""
    listbox = kit.gtk.ListBox(selection_mode=kit.gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")
    rows = {}
    for code in (i18n.AUTOMATIC, *i18n.available()):
        automatic = code == i18n.AUTOMATIC
        row = kit.adw.ActionRow(
            title=_("Automatic (match the desktop)") if automatic else i18n.LANGUAGES[code],
            subtitle="" if automatic else code,
            activatable=True,
        )
        row.check = kit.gtk.Image(icon_name="object-select-symbolic", visible=code == current)
        row.add_suffix(row.check)
        row.code = code
        row.connect("activated", on_activate)
        listbox.append(row)
        rows[code] = row
    return listbox, rows


def _searchable_list(kit: Toolkit, entry, listbox, rows: dict):
    """Search entry above a live-filtered list, with an empty state when nothing
    matches -- the HIG chooser layout. Returns the column to place in a dialog."""

    def matches(row):
        needle = fold(entry.get_text().strip())
        return not needle or needle in fold(row.get_title()) or needle in fold(row.code)

    listbox.set_filter_func(matches)
    listbox.matches = matches  # exposed so tests can ask exactly which rows a query keeps
    empty = kit.adw.StatusPage(
        icon_name="edit-find-symbolic",
        title=_("No Results Found"),
        description=_("Try a different search."),
        visible=False,
        vexpand=True,
    )

    def on_changed(_entry):
        listbox.invalidate_filter()
        any_shown = any(matches(row) for row in rows.values())
        listbox.set_visible(any_shown)
        empty.set_visible(not any_shown)

    entry.connect("search-changed", on_changed)
    column = kit.gtk.Box(orientation=kit.gtk.Orientation.VERTICAL, spacing=12)
    for margin in ("start", "end", "top", "bottom"):
        getattr(column, f"set_margin_{margin}")(12)
    column.append(entry)
    column.append(listbox)
    column.append(empty)
    return column


def language_dialog(kit: Toolkit, actions: Actions, on_choose: Callable[[str], None]):
    """A language chooser built the way GNOME Settings builds its own.

    The HIG pattern for picking from a long list: a search entry at the top, focused on
    open; a full-width list beneath it, filtered live as you type; the current choice
    marked with a check; an empty state when nothing matches; and activating a row
    applies it and closes. A combo popover is for a handful of entries, not ~100.
    """
    dialog = kit.adw.Dialog(title=_("Language"), content_width=520, content_height=640)
    header = kit.adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
    close = kit.gtk.Button(label=_("Close"))
    close.connect("clicked", lambda _b: _close(dialog))
    header.pack_start(close)
    entry = kit.gtk.SearchEntry(placeholder_text=_("Search languages"))
    listbox, rows = _language_rows(
        kit,
        i18n.read_language(actions.directory),
        lambda row: (on_choose(row.code), _close(dialog)),
    )
    column = _searchable_list(kit, entry, listbox, rows)
    scrolled = kit.gtk.ScrolledWindow(child=kit.adw.Clamp(child=column, maximum_size=600))
    scrolled.set_policy(kit.gtk.PolicyType.NEVER, kit.gtk.PolicyType.AUTOMATIC)
    view = kit.adw.ToolbarView(content=scrolled)
    view.add_top_bar(header)
    dialog.set_child(view)
    # Focus lands in the search entry, so opening the chooser and typing just works.
    dialog.set_focus(entry)
    dialog.search_entry = entry
    dialog.rows = rows
    dialog.listbox = listbox
    return dialog


def _dialog_with_close(kit: Toolkit, title: str, page):
    """A read-only dialog: a header with a single Close button above the content."""
    dialog = kit.adw.Dialog(title=title, content_width=520, content_height=640)
    header = kit.adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
    close = kit.gtk.Button(label=_("Close"))
    close.connect("clicked", lambda _b: _close(dialog))
    header.pack_start(close)
    view = kit.adw.ToolbarView(content=page)
    view.add_top_bar(header)
    dialog.set_child(view)
    dialog.header = header  # so a caller can add a search toggle beside Close
    return dialog


def open_for(key: str, kit: Toolkit, actions: Actions, parent, on_done: Callable[[], None]):
    """Open the dialog behind an option row; returns what was opened (for tests)."""
    if key == "credential":
        opened = credential_form(kit, actions, on_done)
    elif key == "network":
        opened = network_form(kit, actions, on_done)
    elif key == "diagnostic":
        opened = diagnostic_dialog(kit, actions)
    elif key == "about":
        opened = about_dialog(kit, actions)
    else:
        # The service row has no dialog: its switches live in the main window.
        return None
    (opened.dialog if isinstance(opened, Form) else opened).present(parent)
    return opened
