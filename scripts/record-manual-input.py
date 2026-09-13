#!/usr/bin/env python3
"""Merge an attended RealVNC input screenshot into an automated scenario record."""

import argparse
import json
from pathlib import Path

from wayland_vnc.manual_input import merge_manual_input
from wayland_vnc.qualification import artifact_integrity_errors, validate_record

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--record", type=Path, required=True)
parser.add_argument("--screenshot", type=Path, required=True)
parser.add_argument("--evidence-root", type=Path, required=True)
parser.add_argument("--commit", required=True, help="must equal the record's commit")
parser.add_argument("--gestures", action="store_true", help="also require scroll/drag markers")
args = parser.parse_args()
record = json.loads(args.record.read_text(encoding="utf-8"))
if record.get("commit") != args.commit:
    parser.error("the record belongs to a different commit than the attended session")
record = merge_manual_input(record, args.screenshot, args.evidence_root, gestures=args.gestures)
args.record.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
errors = validate_record(record, args.commit, args.evidence_root)
print(json.dumps({"status": record["status"], "validation_errors": errors}, indent=2))
# A record the merge marked `passed` must satisfy EVERY check: exiting 0 while
# validate_record still reports errors is how unqualified evidence reaches a release.
# For an incomplete record only integrity failures are fatal (a referenced file that
# is malformed, escapes the evidence root, is missing, or differs in hash or size),
# because the missing scenarios are the very thing that makes it incomplete. Asked of
# the integrity check itself, not read off the messages' wording.
if record["status"] == "passed":
    clean = not errors
else:
    clean = not artifact_integrity_errors(record.get("artifacts"), args.evidence_root)
raise SystemExit(0 if clean else 1)
