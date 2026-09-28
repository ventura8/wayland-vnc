"""Verify an upstream archive and apply its ordered candidate patches in isolation."""

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def confined(argument: Path) -> Path:
    """`argument` as an absolute path, refused unless it lies under the working
    directory, this checkout or the system temporary directory, so a mistyped or
    generated argument cannot reach anything else on the machine."""
    path = argument.resolve()
    allowed = (Path.cwd().resolve(), ROOT, Path(tempfile.gettempdir()).resolve())
    if not any(path.is_relative_to(root) for root in allowed):
        raise SystemExit(f"{argument}: outside the working directory, checkout and temp dir")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=["gnome-remote-desktop", "libvncserver", "tigervnc"])
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    patch_tool = shutil.which("patch")
    if patch_tool is None:
        parser.error("The patch executable is required")
    root = ROOT
    archive_path = confined(args.archive)
    manifest = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    source = next(item for item in manifest["sources"] if item["name"] == args.name)
    with archive_path.open("rb") as archive:
        digest = hashlib.file_digest(archive, "sha256").hexdigest()
    if digest != source["sha256"]:
        parser.error("Archive SHA-256 differs from the source manifest")
    builds = root / "artifacts" / "sources"
    builds.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix=args.name + "-", dir=builds))
    with tarfile.open(archive_path) as archive:
        archive.extractall(destination, filter="data")
    extracted = list(destination.iterdir())
    if len(extracted) != 1 or not extracted[0].is_dir():
        parser.error("Expected one upstream source directory")
    for patch in source["patches"]:
        subprocess.run(
            [
                patch_tool,
                "--batch",
                "--forward",
                "--fuzz=0",
                "-p1",
                "-i",
                str(root / "patches" / args.name / patch),
            ],
            cwd=extracted[0],
            check=True,
            timeout=30,
        )
    print(extracted[0])


if __name__ == "__main__":
    main()
