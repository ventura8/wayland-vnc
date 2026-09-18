#!/usr/bin/env python3
"""Fail unless every required target/viewer pair has valid qualification evidence."""

import argparse
import json
from pathlib import Path

import jsonschema

from wayland_vnc.qualification import release_errors

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--records", type=Path, default=Path("qualification/records.json"))
parser.add_argument("--schema", type=Path, default=Path("qualification/schema-v2.json"))
parser.add_argument("--evidence-root", type=Path, required=True)
parser.add_argument("--commit", required=True)
args = parser.parse_args()

records = json.loads(args.records.read_text(encoding="utf-8"))
if not isinstance(records, list):
    parser.error("records file must contain a JSON array")

# The semantic checks below assume a record of the right SHAPE. Without this, a record
# carrying an unknown field, a mistyped timestamp or a missing block reaches them and
# is judged only on the keys they happen to read. The schema says additionalProperties
# is false, so it is also what rejects anything smuggled into a record.
schema = json.loads(args.schema.read_text(encoding="utf-8"))
format_checker = jsonschema.Draft202012Validator.FORMAT_CHECKER
# jsonschema checks "format": "date-time" only when rfc3339-validator is importable;
# without it the check is silently skipped, so a gate without it fails closed here.
if "date-time" not in format_checker.checkers:
    parser.error("date-time validation is unavailable: install rfc3339-validator")
validator = jsonschema.Draft202012Validator(schema, format_checker=format_checker)
errors = []
for index, record in enumerate(records):
    for failure in sorted(validator.iter_errors(record), key=str):
        location = "/".join(str(part) for part in failure.absolute_path) or "(root)"
        errors.append(f"record {index} fails schema at {location}: {failure.message}")
# A non-object element is already reported by the schema above; the semantic checks
# read record keys, so they only ever see objects.
errors += release_errors(
    [record for record in records if isinstance(record, dict)], args.commit, args.evidence_root
)
print(json.dumps({"schema_version": 2, "passed": not errors, "errors": errors}, indent=2))
raise SystemExit(bool(errors))
