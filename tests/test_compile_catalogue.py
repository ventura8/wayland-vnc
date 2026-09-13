"""The catalogue compiler that packaging actually runs.

Packaging containers have Python but not the gettext tools, so the .mo files users
receive are produced by `scripts/compile_catalogue.py` rather than by msgfmt. That
makes this compiler part of the shipped product, and it is tested as such -- including
against msgfmt itself, so "compiles" means "agrees with the reference implementation".
"""

import gettext
import importlib.util
import struct
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compile_catalogue", REPO / "scripts" / "compile_catalogue.py"
)
catalogue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalogue)

CATALOGUES = sorted((REPO / "po").glob("*.po"))
# An empty glob would turn every parametrised test below into a silent no-op, so
# the suite would report success having compiled nothing at all.
assert CATALOGUES, f"no catalogues found under {REPO / 'po'}; nothing would be tested"
HEADER = 'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n'


def _load(path: Path) -> dict:
    with path.open("rb") as handle:
        return gettext.GNUTranslations(handle)._catalog


@pytest.mark.parametrize("source", CATALOGUES, ids=lambda p: p.stem)
def test_compiled_catalogue_matches_msgfmt_exactly(source, tmp_path):
    """The reference implementation decides what correct means."""
    mine = tmp_path / "mine.mo"
    theirs = tmp_path / "theirs.mo"
    catalogue.compile_catalogue(source, mine)
    subprocess.run(["msgfmt", "--output-file", str(theirs), str(source)], check=True)
    assert _load(mine) == _load(theirs)


def test_a_compiled_catalogue_actually_translates(tmp_path):
    """End to end: what gettext reads back is what the .po said."""
    source = tmp_path / "ro.po"
    source.write_text(HEADER + '\nmsgid "Save"\nmsgstr "Salvează"\n', encoding="utf-8")
    target = tmp_path / "ro.mo"
    catalogue.compile_catalogue(source, target)
    with target.open("rb") as handle:
        assert gettext.GNUTranslations(handle).gettext("Save") == "Salvează"


def test_the_binary_header_is_the_gnu_one(tmp_path):
    source = tmp_path / "x.po"
    source.write_text(HEADER + '\nmsgid "a"\nmsgstr "b"\n', encoding="utf-8")
    target = tmp_path / "x.mo"
    catalogue.compile_catalogue(source, target)
    magic, revision, count = struct.unpack("<3I", target.read_bytes()[:12])
    assert magic == catalogue.MAGIC
    assert revision == 0
    assert count == 2, "the header entry counts too"


def test_entries_are_sorted_so_a_reader_can_binary_search(tmp_path):
    """GNU gettext binary-searches the original table; unsorted entries are not found."""
    source = tmp_path / "s.po"
    source.write_text(
        HEADER + '\nmsgid "zebra"\nmsgstr "Z"\n\nmsgid "apple"\nmsgstr "A"\n', encoding="utf-8"
    )
    target = tmp_path / "s.mo"
    catalogue.compile_catalogue(source, target)
    blob = target.read_bytes()
    _, _, count, originals, _, _, _ = struct.unpack("<7I", blob[:28])
    keys = []
    for index in range(count):
        length, offset = struct.unpack(
            "<II", blob[originals + index * 8 : originals + index * 8 + 8]
        )
        keys.append(blob[offset : offset + length])
    assert keys == sorted(keys)


def test_a_fuzzy_entry_is_left_out_so_the_original_shows_instead(tmp_path):
    """A fuzzy string is a guess; showing it would be worse than showing English."""
    source = tmp_path / "f.po"
    source.write_text(
        HEADER
        + '\n#, fuzzy\nmsgid "Save"\nmsgstr "Wrong guess"\n\nmsgid "Close"\nmsgstr "Inchide"\n',
        encoding="utf-8",
    )
    target = tmp_path / "f.mo"
    catalogue.compile_catalogue(source, target)
    loaded = _load(target)
    assert "Save" not in loaded, "a fuzzy guess must not reach users"
    assert loaded["Close"] == "Inchide", "the entry after a fuzzy one is unaffected"


def test_an_untranslated_entry_is_left_out(tmp_path):
    source = tmp_path / "e.po"
    source.write_text(HEADER + '\nmsgid "Save"\nmsgstr ""\n', encoding="utf-8")
    target = tmp_path / "e.mo"
    catalogue.compile_catalogue(source, target)
    assert "Save" not in _load(target)


def test_multi_line_strings_are_joined(tmp_path):
    """msgmerge wraps long strings; the pieces must come back as one message."""
    source = tmp_path / "w.po"
    source.write_text(
        HEADER + '\nmsgid ""\n"one "\n"two"\nmsgstr ""\n"unu "\n"doi"\n', encoding="utf-8"
    )
    target = tmp_path / "w.mo"
    catalogue.compile_catalogue(source, target)
    assert _load(target)["one two"] == "unu doi"


@pytest.mark.parametrize(
    "escaped,expected",
    [("\\n", "\n"), ("\\t", "\t"), ('\\"', '"'), ("\\\\", "\\")],
)
def test_escape_sequences_are_decoded(tmp_path, escaped, expected):
    source = tmp_path / "esc.po"
    source.write_text(HEADER + f'\nmsgid "k"\nmsgstr "a{escaped}b"\n', encoding="utf-8")
    target = tmp_path / "esc.mo"
    catalogue.compile_catalogue(source, target)
    assert _load(target)["k"] == f"a{expected}b"


def test_non_ascii_survives_the_round_trip(tmp_path):
    """Most of the shipped languages are not Latin script."""
    source = tmp_path / "u.po"
    source.write_text(
        HEADER + '\nmsgid "Language"\nmsgstr "言語 · اللغة · Ελληνικά"\n', encoding="utf-8"
    )
    target = tmp_path / "u.mo"
    catalogue.compile_catalogue(source, target)
    assert _load(target)["Language"] == "言語 · اللغة · Ελληνικά"


def test_plural_forms_are_refused_rather_than_silently_dropped(tmp_path):
    """Nothing here uses them; a silent drop would lose a message without warning."""
    source = tmp_path / "p.po"
    source.write_text(
        HEADER + '\nmsgid "one"\nmsgid_plural "many"\nmsgstr[0] "unu"\nmsgstr[1] "multe"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="plural"):
        catalogue.compile_catalogue(source, tmp_path / "p.mo")


def test_a_malformed_line_is_refused(tmp_path):
    source = tmp_path / "bad.po"
    source.write_text(HEADER + "\nthis is not a po line\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unrecognised"):
        catalogue.compile_catalogue(source, tmp_path / "bad.mo")


def test_the_cli_reports_a_bad_catalogue_instead_of_raising(tmp_path, capsys):
    source = tmp_path / "bad.po"
    source.write_text("nonsense\n", encoding="utf-8")
    assert catalogue.main([str(source), str(tmp_path / "out.mo")]) == 1
    assert "unrecognised" in capsys.readouterr().err
