#!/usr/bin/env python3
"""Summarize one private actual-viewer run as partial evidence, never a qualification."""

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", type=Path, help="private run directory under artifacts/")
parser.add_argument("--fixture", required=True)
parser.add_argument("--compositor", required=True)
parser.add_argument("--backend", required=True)
parser.add_argument("--viewer", required=True)
parser.add_argument("--mode", default="1920x1080")
parser.add_argument("--passed", nargs="*", default=[])
parser.add_argument("--note", required=True)
args = parser.parse_args()

git = ["git", "rev-parse", "HEAD"]
commit = subprocess.run(git, capture_output=True, text=True, check=True).stdout.strip()
status = ["git", "status", "--porcelain"]
dirty = bool(subprocess.run(status, capture_output=True, text=True, check=True).stdout.strip())
artifacts = {}
for path in sorted(args.directory.iterdir()):
    if path.suffix in {".png", ".json"} and path.name != "partial-evidence.json":
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        artifacts[path.name] = {"sha256": digest, "size": path.stat().st_size}
record = {
    "kind": "partial-viewer-evidence",
    "status": "incomplete",
    "note": args.note,
    "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "commit": commit,
    "working_tree_dirty": dirty,
    "fixture": args.fixture,
    "compositor": args.compositor,
    "backend": args.backend,
    "viewer": args.viewer,
    "output_mode": args.mode,
    "checks": dict.fromkeys(args.passed, "passed"),
    "artifacts": artifacts,
}
target = args.directory / "partial-evidence.json"
target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
target.chmod(0o600)
print(json.dumps({"written": str(target), "checks": record["checks"]}, sort_keys=True))
