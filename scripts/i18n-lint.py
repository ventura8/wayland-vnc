#!/usr/bin/env python3
"""Fail the build on a missing, fuzzy, empty or malformed translation.

Runs in the lint stage of the local pipeline and CI. A translation gap is a defect a
user sees -- the window falls back to English for that one string -- and nothing
else in the pipeline would catch it, so this lint is strict on purpose:

* every language `i18n.LANGUAGES` offers has a catalogue in `po/`, and every
  catalogue in `po/` is offered (a file nobody can choose is dead weight);
* every catalogue carries every msgid in the template, with a non-empty msgstr and
  no `fuzzy` flag (a fuzzy entry is a guess msgmerge made, not a translation);
* every `%s` / `%d` / `%(name)s` placeholder in a msgid survives in its msgstr,
  because a dropped one crashes the window at render time;
* `msgfmt --check --check-format` accepts every catalogue, where msgfmt exists.

Exit status is the number of problems, capped at 1, so a shell gate reads it plainly.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PO_DIR = ROOT / "po"
TEMPLATE = PO_DIR / "wayland-vnc.pot"
PLACEHOLDER = re.compile(r"%(?:\([a-z_]+\))?[sd]")
# The comment block before an entry, in whatever order gettext wrote it: `#.`
# extracted comments, `#:` references and `#,` flags all appear, and the flag line is
# normally LAST. An earlier pattern expected `#,` first, so on a real catalogue the
# match simply began at the msgid line with no flags captured -- and every fuzzy entry
# went unnoticed, which is the one thing this lint exists to catch. Obsolete `#~`
# entries stay excluded: their msgid is not at the start of a line.
ENTRY = re.compile(
    r'^(?P<comments>(?:#[^~\n]*\n)*)msgid "(?P<id>(?:[^"\\]|\\.)*)"\n'
    r'(?P<idcont>(?:"(?:[^"\\]|\\.)*"\n)*)msgstr "(?P<str>(?:[^"\\]|\\.)*)"\n'
    r'(?P<strcont>(?:"(?:[^"\\]|\\.)*"\n)*)',
    re.M,
)


def _is_fuzzy(comments: str) -> bool:
    """True when the entry carries gettext's `fuzzy` flag, wherever the line sits."""
    return any(
        line.startswith("#,") and "fuzzy" in line.split(",")[1:]
        for line in (c.strip().replace(" ", "") for c in comments.splitlines())
    )


def entries(path: Path) -> dict[str, tuple[str, bool]]:
    """msgid -> (msgstr, fuzzy) for every non-header entry, joining wrapped strings."""
    found = {}
    for match in ENTRY.finditer(path.read_text(encoding="utf-8")):
        msgid = match["id"] + "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', match["idcont"]))
        msgstr = match["str"] + "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', match["strcont"]))
        if msgid:
            found[msgid] = (msgstr, _is_fuzzy(match["comments"]))
    return found


def offered_languages() -> set[str]:
    """The codes `i18n.LANGUAGES` lists, read from the module without importing GTK."""
    source = (ROOT / "src" / "wayland_vnc" / "i18n.py").read_text(encoding="utf-8")
    start = source.index("LANGUAGES: dict[str, str] = {")
    block = source[start : source.index("\n}\n", start)]
    return set(re.findall(r'^\s*"([A-Za-z_]+)":', block, re.M))


def lint() -> list[str]:
    problems: list[str] = []
    template = entries(TEMPLATE)
    catalogues = {path.stem: path for path in PO_DIR.glob("*.po")}
    offered = offered_languages()
    for code in sorted(offered - set(catalogues)):
        problems.append(f"{code}: offered in i18n.LANGUAGES but po/{code}.po does not exist")
    for code in sorted(set(catalogues) - offered):
        problems.append(f"{code}: po/{code}.po exists but i18n.LANGUAGES does not offer it")
    msgfmt = shutil.which("msgfmt")
    for code, path in sorted(catalogues.items()):
        found = entries(path)
        for msgid in template:
            if msgid not in found:
                problems.append(f"{code}: missing {msgid!r}")
                continue
            msgstr, fuzzy = found[msgid]
            if fuzzy:
                problems.append(f"{code}: fuzzy {msgid!r}")
            if not msgstr.strip():
                problems.append(f"{code}: untranslated {msgid!r}")
            elif sorted(PLACEHOLDER.findall(msgstr)) != sorted(PLACEHOLDER.findall(msgid)):
                problems.append(f"{code}: placeholders differ in {msgid!r} -> {msgstr!r}")
        if msgfmt:
            result = subprocess.run(
                [msgfmt, "--check", "--check-format", "-o", "/dev/null", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                problems.append(f"{code}: msgfmt: {result.stderr.strip()}")
    return problems


def main() -> int:
    problems = lint()
    for problem in problems:
        print(problem, file=sys.stderr)
    count = len(list(PO_DIR.glob("*.po")))
    if problems:
        print(f"i18n-lint: {len(problems)} problem(s) across {count} catalogues", file=sys.stderr)
        return 1
    print(f"i18n-lint: {count} catalogues complete, {len(entries(TEMPLATE))} messages each")
    return 0


if __name__ == "__main__":
    sys.exit(main())
