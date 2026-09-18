"""Enforce per-file coverage, not just an aggregate that hides untested modules."""

import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
failures = [
    name for name, details in data["files"].items() if details["summary"]["percent_covered"] < 90
]
if failures:
    sys.exit("Below 90% per-file coverage: " + ", ".join(failures))
