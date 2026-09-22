#!/usr/bin/env python3
"""Compile a gettext .po catalogue into the binary .mo the runtime loads.

Packaging runs inside minimal containers -- Fedora, AlmaLinux, openSUSE, Arch, and
the Flatpak and Snap build sandboxes -- that carry Python (the payload is Python) but
not necessarily the gettext tools. Shelling out to `msgfmt` there made every one of
those builds fail on a missing binary, so the catalogues that reach users are built
by this instead, and `msgfmt --check` stays in the lint gate where gettext is present.

The format is the GNU one: a header, a table of original strings and a table of
translations, both sorted by msgid so a reader can binary-search them. The optional
hash table is omitted, which readers handle by falling back to that binary search.
"""

import argparse
import struct
import sys
from pathlib import Path

MAGIC = 0x950412DE
ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    '"': '"',
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
}


def unquote(line: str) -> str:
    """Decode one `"..."` fragment of a .po file into the text it represents."""
    body = line.strip()
    if not (body.startswith('"') and body.endswith('"')):
        raise ValueError(f"expected a quoted string, got: {line!r}")
    text, index = [], 1
    end = len(body) - 1
    while index < end:
        char = body[index]
        if char == "\\":
            index += 1
            nxt = body[index]
            text.append(ESCAPES.get(nxt, nxt))
        else:
            text.append(char)
        index += 1
    return "".join(text)


def parse(text: str) -> dict[str, str]:
    """Read a .po file into msgid -> msgstr.

    Fuzzy entries and entries with an empty translation are left out, exactly as
    msgfmt does: both mean "no translation yet", and including them would hand the
    reader a wrong or blank string instead of letting it fall back to the original.
    The header (msgid "") is kept even when marked fuzzy, because it carries the
    charset the reader needs.
    """
    entries: dict[str, str] = {}
    msgid: str | None = None
    msgstr: str | None = None
    field: str | None = None
    # A `#, fuzzy` comment applies to the entry that follows it, not the one above.
    pending_fuzzy = False
    fuzzy = False
    plural = False

    def flush() -> None:
        if msgid is None:
            return
        # Checked before the msgstr guard: a plural entry never fills msgstr, so the
        # guard alone would drop the message silently instead of reporting it.
        if plural:
            raise ValueError(f"plural forms are not supported: {msgid!r}")
        if msgstr is None:
            return
        if msgid == "" or (msgstr != "" and not fuzzy):
            entries[msgid] = msgstr

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            if line.startswith("#,") and "fuzzy" in line:
                pending_fuzzy = True
            continue
        if not line:
            continue
        if line.startswith("msgctxt "):
            raise ValueError("message contexts are not supported")
        if line.startswith(("msgid_plural", "msgstr[")):
            plural = True
            field = None
        elif line.startswith("msgid "):
            flush()
            msgid, msgstr, field = unquote(line[6:]), None, "id"
            fuzzy, pending_fuzzy, plural = pending_fuzzy, False, False
        elif line.startswith("msgstr "):
            msgstr, field = unquote(line[7:]), "str"
        elif line.startswith('"'):
            # A continuation of whichever field was last opened.
            if field == "id":
                msgid += unquote(line)
            elif field == "str":
                msgstr += unquote(line)
        else:
            raise ValueError(f"unrecognised line: {raw!r}")
    flush()
    return entries


def serialise(entries: dict[str, str]) -> bytes:
    """Lay the catalogue out in the binary format, msgids sorted for binary search."""
    items = sorted((k.encode(), v.encode()) for k, v in entries.items())
    count = len(items)
    # header, then two (length, offset) tables of 8 bytes per entry
    originals_table = 7 * 4
    translations_table = originals_table + count * 8
    payload = translations_table + count * 8

    offset = payload
    originals, blobs = [], []
    for key, _value in items:
        originals.append((len(key), offset))
        blobs.append(key + b"\0")
        offset += len(key) + 1
    translations = []
    for _key, value in items:
        translations.append((len(value), offset))
        blobs.append(value + b"\0")
        offset += len(value) + 1

    header = struct.pack("<7I", MAGIC, 0, count, originals_table, translations_table, 0, 0)
    tables = b"".join(
        struct.pack("<II", length, position) for length, position in originals + translations
    )
    return header + tables + b"".join(blobs)


def compile_catalogue(source: Path, destination: Path) -> int:
    """Compile `source` to `destination`, returning how many messages were written."""
    entries = parse(source.read_text(encoding="utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(serialise(entries))
    return len(entries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="the .po catalogue to compile")
    parser.add_argument("destination", type=Path, help="the .mo file to write")
    args = parser.parse_args(argv)
    try:
        written = compile_catalogue(args.source, args.destination)
    except (OSError, ValueError) as error:
        print(f"{args.source}: {error}", file=sys.stderr)
        return 1
    print(f"{args.source.name}: {written} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
