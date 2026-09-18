"""Synthetic unit records test the validator, not product compatibility."""

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from wayland_vnc.qualification import (
    PORTAL_SCENARIOS,
    SCENARIOS,
    SCHEMA_VERSION,
    TARGETS,
    VERSION_FIELDS,
    VIEWERS,
    artifact_entry,
    artifact_integrity_errors,
    new_partial_record,
    release_errors,
    validate_artifacts,
    validate_record,
)

REPO = Path(__file__).resolve().parent.parent
SCHEMA = REPO / "qualification" / "schema-v2.json"


def record(tmp_path, target="gnome", viewer="realvnc-android"):
    data = b"UNIT TEST ONLY - NOT REAL VIEWER EVIDENCE"
    (tmp_path / "synthetic.txt").write_bytes(data)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "recorded_at": "2026-09-14T08:00:00Z",
        "commit": "unit-test-commit",
        "target": target,
        "viewer": viewer,
        "session_type": "wayland",
        "status": "passed",
        # A record only qualifies if the fixture was still healthy after the run.
        "post_run_smoke": "passed",
        "versions": dict.fromkeys(VERSION_FIELDS, "unit-test-version"),
        "scenarios": dict.fromkeys(SCENARIOS + PORTAL_SCENARIOS, "passed"),
        "first_frame_seconds": 1.0,
        "reconnect_cycles": 20,
        "artifacts": [
            {
                "path": "synthetic.txt",
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "kind": kind,
            }
            for kind in ("viewer-capture", "input-results", "runner-log")
        ],
    }


def test_all_targets_required(tmp_path):
    assert len(release_errors([], "unit-test-commit", tmp_path)) == 14
    records = [record(tmp_path, target, viewer) for target in TARGETS for viewer in VIEWERS]
    assert not release_errors(records, "unit-test-commit", tmp_path)
    records.append(records[0])
    assert "duplicate" in release_errors(records, "unit-test-commit", tmp_path)[0]


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema_version", 1),
        ("run_id", "not-a-uuid"),
        ("recorded_at", "not-a-timestamp"),
        ("recorded_at", "2026-09-14T08:00:00"),
        ("commit", "stale"),
        ("target", "weston"),
        ("viewer", "generic-vnc"),
        ("session_type", "x11"),
        ("status", "skipped"),
        ("versions", {}),
        ("versions", None),
        ("scenarios", {}),
        ("scenarios", None),
        ("first_frame_seconds", 11),
        ("first_frame_seconds", True),
        ("first_frame_seconds", float("nan")),
        ("reconnect_cycles", 19),
        ("reconnect_cycles", "20"),
        ("artifacts", []),
        ("artifacts", None),
        ("artifacts", [None]),
        ("artifacts", [{"path": "../outside"}]),
        ("artifacts", [{"path": "missing"}]),
        ("artifacts", [{"path": "synthetic.txt", "sha256": "wrong", "kind": "runner-log"}]),
    ],
)
def test_fail_closed(tmp_path, key, value):
    result = record(tmp_path)
    result[key] = value
    assert validate_record(result, "unit-test-commit", tmp_path)
    assert release_errors([result], "unit-test-commit", tmp_path)


def test_portal_scenarios_required_for_plasma(tmp_path):
    result = record(tmp_path, "plasma")
    del result["scenarios"]["portal-deny"]
    assert validate_record(result, "unit-test-commit", tmp_path)


def test_artifact_entry_is_content_addressed(tmp_path):
    artifact = tmp_path / "capture.png"
    artifact.write_bytes(b"pixels")
    entry = artifact_entry(artifact, tmp_path, "viewer-capture")
    assert entry == {
        "path": "capture.png",
        "sha256": hashlib.sha256(b"pixels").hexdigest(),
        "size": 6,
        "kind": "viewer-capture",
    }


def test_artifact_entry_rejects_escape_missing_and_symlink(tmp_path):
    outside = tmp_path.parent / "outside-evidence"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="inside"):
        artifact_entry(outside, tmp_path, "runner-log")
    with pytest.raises(ValueError, match="inside"):
        artifact_entry(tmp_path / "missing", tmp_path, "runner-log")
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlinks"):
        artifact_entry(link, tmp_path, "runner-log")


def test_artifact_integrity_covers_every_way_a_file_can_differ(tmp_path):
    """The attended-input merge exits non-zero on ANY integrity failure of an
    incomplete record, so the integrity check must report a size mismatch as
    readily as a hash mismatch, a missing file or a malformed entry -- and must
    stay silent about completeness, which an incomplete record legitimately lacks."""
    artifact = tmp_path / "capture.png"
    artifact.write_bytes(b"pixels")
    intact = artifact_entry(artifact, tmp_path, "viewer-capture")
    assert artifact_integrity_errors([intact], tmp_path) == []
    assert artifact_integrity_errors([{**intact, "size": 7}], tmp_path) == [
        "artifact size mismatch"
    ]
    assert artifact_integrity_errors([{**intact, "sha256": "0" * 64}], tmp_path) == [
        "artifact hash mismatch"
    ]
    assert artifact_integrity_errors([{**intact, "path": "../elsewhere"}], tmp_path) == [
        "artifact escapes evidence directory or is missing"
    ]
    assert artifact_integrity_errors(["not an entry"], tmp_path) == ["invalid artifact entry"]
    # Completeness is validate_artifacts' verdict alone.
    assert artifact_integrity_errors([], tmp_path) == []
    assert "missing viewer capture, input results, or runner log" in validate_artifacts(
        [intact], tmp_path
    )


def test_partial_record_can_never_validate_as_passing(tmp_path):
    partial = new_partial_record(commit="abc", target="sway", viewer="realvnc-desktop", versions={})
    assert partial["schema_version"] == SCHEMA_VERSION
    assert partial["status"] == "incomplete"
    assert validate_record(partial, "abc", tmp_path)


def test_public_schema_matches_code_constants():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert tuple(schema["properties"]["target"]["enum"]) == TARGETS
    assert tuple(schema["properties"]["viewer"]["enum"]) == VIEWERS


def test_public_schema_accepts_a_runner_shaped_record(tmp_path):
    import jsonschema

    from wayland_vnc.desktop_scenarios import Outcome, Session, fill_record

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    versions = {k: "x" for k in ("compositor", "backend", "viewer", "distribution", "renderer")}
    record = new_partial_record(
        commit="abcdef1", target="sway", viewer="realvnc-desktop", versions=versions
    )
    run_dir = tmp_path / "sway" / "realvnc-desktop" / record["run_id"]
    run_dir.mkdir(parents=True)
    capture = run_dir / "first-frame.png"
    capture.write_bytes(b"synthetic")
    outcomes = {name: Outcome("passed", "ok", [capture]) for name in SCENARIOS}
    outcomes["suspend-resume"] = Outcome("not-run", "needs a VM")
    session = Session(fixture="sway", evidence_dir=run_dir, driver=object())
    session.first_frame_seconds = 0.3
    session.reconnect_cycles = 20
    session.post_run_healthy = True
    record = fill_record(record, session, outcomes, tmp_path)
    # The record the runner writes must validate against the published schema.
    jsonschema.validate(record, schema)
    assert set(record) <= set(schema["properties"])


@pytest.mark.parametrize("smoke", [None, "failed", "skipped", ""])
def test_a_record_without_a_passing_post_run_smoke_never_qualifies(tmp_path, smoke):
    """A fixture that died during the run cannot qualify, whatever `status` claims.

    `status` is written by the producer, so the health check is required in its own
    right rather than trusted through it.
    """
    entry = record(tmp_path)
    if smoke is None:
        entry.pop("post_run_smoke")
    else:
        entry["post_run_smoke"] = smoke
    assert "post-run health check missing or failed" in validate_record(
        entry, "unit-test-commit", tmp_path
    )
