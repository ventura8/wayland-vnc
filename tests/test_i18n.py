"""Translation behaviour, and the completeness of every shipped catalogue.

The catalogue tests read the real `po/` tree rather than fixtures: a language that
ships with a missing or malformed string is a defect users see, and only checking
the actual files can catch it.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from wayland_vnc import i18n

REPO = Path(__file__).resolve().parent.parent
PO_DIR = REPO / "po"
TEMPLATE = PO_DIR / "wayland-vnc.pot"
CATALOGUES = sorted(PO_DIR.glob("*.po"))


# A .po entry may be wrapped across continuation lines, and every catalogue here is
# UTF-8 whatever the ambient locale says, so both are handled explicitly rather than
# relying on --no-wrap and on the reader's default encoding.
_ENTRY = re.compile(
    r'^msgid "((?:[^"\\]|\\.)*)"\n((?:"(?:[^"\\]|\\.)*"\n)*)'
    r'msgstr "((?:[^"\\]|\\.)*)"\n((?:"(?:[^"\\]|\\.)*"\n)*)',
    re.M,
)
_PIECE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _joined(first: str, continuation: str) -> str:
    return first + "".join(_PIECE.findall(continuation))


def _pairs(path: Path) -> list[tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    return [
        (_joined(msgid, idcont), _joined(msgstr, strcont))
        for msgid, idcont, msgstr, strcont in _ENTRY.findall(text)
    ]


def _msgids(path: Path) -> list[str]:
    """Every non-empty msgid in a .po/.pot file, in file order."""
    return [msgid for msgid, _msgstr in _pairs(path) if msgid]


def _entries(path: Path) -> dict[str, str]:
    """msgid -> msgstr for every non-header entry."""
    return {msgid: msgstr for msgid, msgstr in _pairs(path) if msgid}


def test_every_advertised_language_actually_ships_a_catalogue():
    """The settings window offers what `LANGUAGES` lists, so each needs a real file."""
    shipped = {path.stem for path in CATALOGUES}
    assert shipped == set(i18n.LANGUAGES), (
        f"offered but not shipped: {sorted(set(i18n.LANGUAGES) - shipped)}; "
        f"shipped but not offered: {sorted(shipped - set(i18n.LANGUAGES))}"
    )


def test_the_languages_cover_every_inhabited_continent():
    """The point of the set is global reach, not a long list of European languages."""
    by_continent = {
        "Africa": {"af", "am", "ar", "ha", "sw", "yo"},
        "Asia": {"ar", "bn", "fa", "hi", "id", "ja", "ko", "th", "tr", "vi", "zh_CN", "zh_TW"},
        "Europe": {"de", "el", "es", "fr", "it", "nl", "pl", "pt", "ro", "ru", "uk"},
        "Americas": {"es", "fr", "pt_BR"},
        "Oceania": {"id", "mi"},
    }
    offered = set(i18n.LANGUAGES)
    for continent, codes in by_continent.items():
        assert codes & offered, f"no language offered for {continent}"


@pytest.mark.parametrize("catalogue", CATALOGUES, ids=lambda p: p.stem)
def test_catalogue_translates_every_message_with_nothing_left_blank(catalogue):
    """A blank msgstr silently falls back to English, which looks like a broken app."""
    template, entries = set(_msgids(TEMPLATE)), _entries(catalogue)
    assert set(entries) == template, (
        f"{catalogue.stem}: missing {sorted(template - set(entries))[:5]}, "
        f"unexpected {sorted(set(entries) - template)[:5]}"
    )
    blank = sorted(msgid for msgid, msgstr in entries.items() if not msgstr.strip())
    assert not blank, f"{catalogue.stem} leaves {len(blank)} message(s) untranslated: {blank[:5]}"


@pytest.mark.parametrize("catalogue", CATALOGUES, ids=lambda p: p.stem)
def test_catalogue_keeps_every_format_placeholder(catalogue):
    """A dropped %s or %(port)s crashes the window at render time, not at build time."""
    for msgid, msgstr in _entries(catalogue).items():
        expected = sorted(re.findall(r"%(?:\([a-z_]+\))?[sd]", msgid))
        assert sorted(re.findall(r"%(?:\([a-z_]+\))?[sd]", msgstr)) == expected, (
            f"{catalogue.stem}: placeholders differ for {msgid!r} -> {msgstr!r}"
        )


@pytest.mark.parametrize("catalogue", CATALOGUES, ids=lambda p: p.stem)
def test_catalogue_compiles(catalogue):
    """msgfmt is what the packages run; a catalogue that fails it would ship broken."""
    result = subprocess.run(
        ["msgfmt", "--check", "--check-format", "--output-file=/dev/null", str(catalogue)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{catalogue.stem}: {result.stderr.strip()}"


def test_automatic_is_the_default_and_survives_a_missing_preferences_file(tmp_path):
    assert i18n.read_language(tmp_path) == i18n.AUTOMATIC


def test_a_stored_language_is_read_back(tmp_path):
    i18n.write_language(tmp_path, "ro")
    assert i18n.read_language(tmp_path) == "ro"


def test_writing_a_language_leaves_other_preferences_alone(tmp_path):
    path = i18n.preferences_path(tmp_path)
    path.write_text(json.dumps({"language": "de", "something_else": 42}))
    i18n.write_language(tmp_path, "ja")
    assert json.loads(path.read_text()) == {"language": "ja", "something_else": 42}


@pytest.mark.parametrize("content", ["not json at all", '["a list"]', '{"language": "klingon"}'])
def test_unreadable_or_unknown_preferences_fall_back_to_automatic(tmp_path, content):
    """The window must still open; a bad preference is not a reason to fail."""
    i18n.preferences_path(tmp_path).write_text(content)
    assert i18n.read_language(tmp_path) == i18n.AUTOMATIC


def test_writing_an_unknown_language_is_refused(tmp_path):
    with pytest.raises(ValueError, match="Unknown language"):
        i18n.write_language(tmp_path, "klingon")


def test_activate_falls_back_to_english_instead_of_failing(tmp_path):
    """A language with no compiled catalogue must not stop the window opening."""
    assert i18n.activate("ja", localedir=tmp_path) == "ja"
    assert i18n._("Save") == "Save"
    i18n.activate(i18n.AUTOMATIC, localedir=tmp_path)


def test_activate_exports_the_language_to_the_toolkit_and_automatic_hands_it_back(
    tmp_path, monkeypatch
):
    """GTK and libadwaita translate their own strings (About, Shortcuts, Close) via libc
    gettext, which reads LANGUAGE: an explicit choice must reach them, and Automatic
    must restore exactly what the session had -- including its absence."""
    # activate rewrites the process's LANGUAGE; monkeypatch puts the real one back.
    monkeypatch.delenv("LANGUAGE", raising=False)
    monkeypatch.setattr(i18n, "SESSION_LANGUAGE", "de")
    i18n.activate("ro", localedir=tmp_path)
    assert os.environ["LANGUAGE"] == "ro"
    i18n.activate(i18n.AUTOMATIC, localedir=tmp_path)
    assert os.environ["LANGUAGE"] == "de"
    monkeypatch.setattr(i18n, "SESSION_LANGUAGE", None)
    i18n.activate("ja", localedir=tmp_path)
    assert os.environ["LANGUAGE"] == "ja"
    i18n.activate(i18n.AUTOMATIC, localedir=tmp_path)
    assert "LANGUAGE" not in os.environ


def test_activate_reports_automatic_for_every_way_of_asking_for_it(tmp_path):
    for request in (None, "", i18n.AUTOMATIC):
        assert i18n.activate(request, localedir=tmp_path) == i18n.AUTOMATIC
        assert i18n.active_language() == i18n.AUTOMATIC


def test_available_lists_only_languages_with_a_compiled_catalogue(tmp_path):
    assert i18n.available(tmp_path) == []
    built = tmp_path / "ro" / "LC_MESSAGES"
    built.mkdir(parents=True)
    (built / f"{i18n.DOMAIN}.mo").write_bytes(b"")
    assert i18n.available(tmp_path) == ["ro"]


def test_locale_dir_honours_the_environment_override(monkeypatch, tmp_path):
    """Relocatable bundles decide their prefix at run time, not at build time."""
    monkeypatch.setenv("WAYLAND_VNC_LOCALEDIR", str(tmp_path))
    assert i18n.locale_dir() == tmp_path


@pytest.mark.parametrize(
    "language,expected", [("ar", True), ("fa", True), ("de", False), ("ja", False)]
)
def test_right_to_left_languages_are_recognised(language, expected):
    assert i18n.is_rtl(language) is expected


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"LANG": "ar_EG.UTF-8"}, True),
        ({"LANGUAGE": "fa"}, True),
        # LANGUAGE is a colon-separated preference list; the first entry decides.
        ({"LANGUAGE": "ar:en_US:en"}, True),
        ({"LANGUAGE": "he_IL.UTF-8:en"}, True),
        ({"LANGUAGE": "en:ar"}, False),
        ({"LANG": "de_DE.UTF-8"}, False),
        ({}, False),
    ],
)
def test_automatic_takes_its_direction_from_the_locale_environment(env, expected):
    assert i18n.is_rtl(i18n.AUTOMATIC, env=env) is expected


def test_the_marker_returns_its_message_unchanged():
    """The marker only tags strings for extraction; the CLI's JSON must not move."""
    assert i18n.translatable("Passwords did not match") == "Passwords did not match"
