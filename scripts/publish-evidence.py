#!/usr/bin/env python3
"""Publish one commit's qualification evidence into the evidence store the release
gate reads (a clone of the private evidence repository).

    scripts/publish-evidence.py --store ../wayland-vnc-evidence [--commit SHA] [--push]

Only records that validate for the commit are published unless --include-incomplete
is given; every artifact is hash-checked before it is copied. With --push the store
clone is committed and pushed with the runner's own git credentials; without it the
files are staged in the clone for the maintainer to look at first.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from wayland_vnc.evidence_store import publish
from wayland_vnc.qualification import TARGETS, VIEWERS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-root", type=Path, default=Path("artifacts/qualification-evidence")
    )
    parser.add_argument(
        "--store", type=Path, required=True, help="clone of the evidence repository"
    )
    parser.add_argument("--commit", help="full commit hash (default: HEAD of this checkout)")
    parser.add_argument("--include-incomplete", action="store_true")
    parser.add_argument("--push", action="store_true", help="commit and push the store clone")
    args = parser.parse_args(argv)

    commit = (
        args.commit
        or subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=30
        ).stdout.strip()
    )
    if not (args.store / ".git").exists():
        parser.error(f"{args.store} is not a git clone of the evidence repository")

    result = publish(
        args.evidence_root, args.store, commit, include_incomplete=args.include_incomplete
    )
    for line in result.skipped:
        print(f"skipped: {line}", file=sys.stderr)
    print(
        f"published {len(result.records)} record(s), {len(result.copied)} artifact(s) for {commit}"
    )
    missing = [f"{t}/{v}" for t in TARGETS for v in VIEWERS if (t, v) not in result.pairs]
    detail = f" ({', '.join(missing)})" if missing else ""
    print(f"pairs still without a published record: {len(missing)}{detail}")
    if not result.records:
        print("nothing to publish for this commit", file=sys.stderr)
        return 1
    if args.push:
        git = ["git", "-C", str(args.store)]
        subprocess.run([*git, "add", "--", commit], check=True, timeout=120)
        message = f"evidence for {commit}: {len(result.records)} record(s)"
        subprocess.run([*git, "commit", "--quiet", "-m", message], check=True, timeout=120)
        subprocess.run([*git, "push", "--quiet"], check=True, timeout=600)
        print("pushed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
