"""Merge attended input evidence (a person typing, clicking, scrolling, dragging in the
actual viewer) into an automated scenario record. The screenshot must carry the scene's
own acknowledgement markers; nothing is taken on trust from the operator."""

import json
import shutil
from pathlib import Path

from wayland_vnc.image_evidence import verify_gesture_markers, verify_input_markers, verify_scene
from wayland_vnc.qualification import PORTAL_SCENARIOS, SCENARIOS, artifact_entry

INPUT_SCENARIOS = ("keyboard", "pointer")
GESTURE_SCENARIOS = ("scroll", "drag")


def merge_manual_input(
    record: dict, screenshot: Path, evidence_root: Path, *, gestures: bool
) -> dict:
    """Return the record with input scenarios passed only if the markers are present."""
    if record.get("status") not in ("incomplete", "passed"):
        raise ValueError("Only incomplete or passed records accept manual evidence")
    root = evidence_root.resolve()
    run_dir = (root / record["target"] / record["viewer"] / record["run_id"]).resolve()
    # The path is built from record fields, so a crafted target/viewer/run_id could
    # contain ".." and steer the writes below out of the evidence tree. artifact_entry
    # rejects an escaping path, but only after the screenshot and results have already
    # been written, so containment is checked here first.
    if not run_dir.is_relative_to(root):
        raise ValueError("The record's run directory escapes the evidence root")
    if not run_dir.is_dir():
        raise ValueError("The record's run directory is missing from the evidence root")
    results = {
        "schema_version": 1,
        "scene": verify_scene(screenshot).as_dict(),
        "input": verify_input_markers(screenshot).as_dict(),
    }
    passed = list(INPUT_SCENARIOS)
    if gestures:
        results["gestures"] = verify_gesture_markers(screenshot).as_dict()
        passed += list(GESTURE_SCENARIOS)
    stored = run_dir / "manual-input.png"
    if screenshot.resolve() != stored:
        shutil.copyfile(screenshot, stored)
    results_path = run_dir / "manual-input-results.json"
    results_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name in passed:
        record["scenarios"][name] = "passed"
        record.setdefault("scenario_details", {})[name] = "attended run; marker verified"
    artifacts = [a for a in record["artifacts"] if a["kind"] != "input-results"]
    artifacts = [a for a in artifacts if not a["path"].endswith("manual-input.png")]
    artifacts.append(artifact_entry(stored, root, "viewer-capture"))
    artifacts.append(artifact_entry(results_path, root, "input-results"))
    record["artifacts"] = artifacts
    healthy = record.get("post_run_smoke") == "passed"
    # Every REQUIRED scenario must be present and passed. Checking only the keys
    # that happen to be in the record would let a record missing half the suite
    # satisfy all() vacuously and be written out as passed.
    required = set(SCENARIOS)
    if record.get("target") == "plasma":
        required.update(PORTAL_SCENARIOS)
    complete = all(record["scenarios"].get(name) == "passed" for name in required)
    record["status"] = "passed" if healthy and complete else "incomplete"
    return record
