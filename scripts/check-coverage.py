"""Enforce per-file coverage, not just an aggregate that hides untested modules."""

import json
import sys
import tempfile
from pathlib import Path

# A path argument must lie under the working directory, this checkout or the system
# temporary directory, so a mistyped or generated argument cannot reach anything else
# on the machine.
CHECKOUT = Path(__file__).resolve().parents[1]


def confined(argument: str | Path) -> Path:
    """`argument` as an absolute path, refused when it lies outside the allowed roots."""
    path = Path(argument).resolve()
    roots = (Path.cwd().resolve(), CHECKOUT, Path(tempfile.gettempdir()).resolve())
    if not any(path.is_relative_to(root) for root in roots):
        raise SystemExit(f"{argument}: outside the working directory, checkout and temp dir")
    return path


data = json.loads(confined(sys.argv[1]).read_text(encoding="utf-8"))
failures = [
    name for name, details in data["files"].items() if details["summary"]["percent_covered"] < 90
]
if failures:
    sys.exit("Below 90% per-file coverage: " + ", ".join(failures))
