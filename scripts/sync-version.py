#!/usr/bin/env python3
"""Write the repo-root VERSION into pyproject.toml [project] version (single source).

The root ``VERSION`` file is the one place the release number lives. This script
copies it into ``pyproject.toml`` so packaging (deb/rpm/arch/appimage/flatpak/snap)
and the Python distribution never drift. ``--check`` fails instead of writing when
they already differ, which the lint gate uses.
"""

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
PYPROJECT = ROOT / "pyproject.toml"
VERSION_RE = re.compile(r'^(version\s*=\s*")([^"]*)(")', re.MULTILINE)
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def read_version() -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not SEMVER_RE.match(version):
        raise SystemExit(f"VERSION must be N.N.N semver, got {version!r}")
    return version


def project_table(text: str) -> tuple[int, int]:
    """The character span of the [project] table, up to the next table header.

    `version = "..."` is matched only inside it, so a version key in an earlier
    table -- a tool's own -- can neither be read as the project version nor rewritten.
    """
    header = re.search(r"^\[project\]\s*$", text, re.MULTILINE)
    if not header:
        raise SystemExit("pyproject.toml has no [project] table")
    start = header.end()
    following = re.search(r"^\[", text[start:], re.MULTILINE)
    return start, start + following.start() if following else len(text)


def current_pyproject_version(text: str) -> str:
    start, end = project_table(text)
    match = VERSION_RE.search(text, start, end)
    if not match:
        raise SystemExit("pyproject.toml has no [project] version line")
    return match.group(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if they differ; write nothing")
    args = parser.parse_args(argv)
    version = read_version()
    text = PYPROJECT.read_text(encoding="utf-8")
    existing = current_pyproject_version(text)
    if existing == version:
        print(f"pyproject.toml already at {version}")
        return 0
    if args.check:
        print(
            f"VERSION={version} but pyproject.toml={existing}; run scripts/sync-version.py",
            file=sys.stderr,
        )
        return 1
    start, end = project_table(text)
    updated = text[:start] + VERSION_RE.sub(rf"\g<1>{version}\g<3>", text[start:end], count=1)
    updated += text[end:]
    # tomllib parses the result before it is written: the one check that the edit
    # landed on the key packaging reads, and left the file valid TOML.
    if tomllib.loads(updated)["project"]["version"] != version:
        raise SystemExit("pyproject.toml rewrite did not land on [project] version")
    PYPROJECT.write_text(updated, encoding="utf-8")
    print(f"pyproject.toml version set to {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
