"""The evidence store carries only what the gate can trust: this commit's records,
with every referenced artifact copied after its hash was checked, in the layout
check-release.py already reads."""

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from wayland_vnc import evidence_store
from wayland_vnc.qualification import (
    PORTAL_SCENARIOS,
    SCENARIOS,
    SCHEMA_VERSION,
    VERSION_FIELDS,
    release_errors,
)

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _artifact(root: Path, relative: str, data: bytes, kind: str) -> dict:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    return {"path": relative, "sha256": digest, "size": len(data), "kind": kind}


def _record(
    root: Path, target: str, viewer: str, *, commit: str = COMMIT, status: str = "passed"
) -> dict:
    run_id = str(uuid.uuid4())
    base = f"{target}/{viewer}/{run_id}"
    record = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "recorded_at": "2026-09-17T10:00:00Z",
        "commit": commit,
        "target": target,
        "viewer": viewer,
        "session_type": "wayland",
        "status": status,
        "post_run_smoke": "passed",
        "versions": dict.fromkeys(VERSION_FIELDS, "unit-test-version"),
        "scenarios": dict.fromkeys(SCENARIOS + PORTAL_SCENARIOS, "passed"),
        "first_frame_seconds": 1.0,
        "reconnect_cycles": 20,
        "artifacts": [
            _artifact(root, f"{base}/first-frame.png", b"PNG" + run_id.encode(), "viewer-capture"),
            _artifact(root, f"{base}/input.json", b"{}", "input-results"),
            _artifact(root, f"{base}/runner.log", b"log", "runner-log"),
        ],
    }
    records_dir = root / "records"
    records_dir.mkdir(exist_ok=True)
    record_path = records_dir / f"{target}-{viewer}-{run_id}.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    return record


def test_publish_copies_this_commits_valid_records_and_their_artifacts(tmp_path):
    root = tmp_path / "evidence"
    store = tmp_path / "store"
    kept = _record(root, "sway", "realvnc-desktop")
    _record(root, "sway", "realvnc-android", commit="ffffffffffffffffffffffffffffffffffffffff")
    _record(root, "wayfire", "realvnc-desktop", status="incomplete")

    result = evidence_store.publish(root, store, COMMIT)

    assert result.pairs == {("sway", "realvnc-desktop")}
    assert len(result.skipped) == 2
    assert any("belongs to commit ffff" in line for line in result.skipped)
    assert any("run did not pass" in line for line in result.skipped)
    published = json.loads((store / COMMIT / "records.json").read_text(encoding="utf-8"))
    assert [record["run_id"] for record in published] == [kept["run_id"]]
    for entry in kept["artifacts"]:
        copy = store / COMMIT / entry["path"]
        assert hashlib.sha256(copy.read_bytes()).hexdigest() == entry["sha256"]
    assert (store / COMMIT / "records" / f"sway-realvnc-desktop-{kept['run_id']}.json").is_file()
    # What the gate will run against the store is the same call it runs locally.
    errors = release_errors(published, COMMIT, store / COMMIT)
    assert not any("sway/realvnc-desktop" in error for error in errors)
    assert len([error for error in errors if error.startswith("missing qualification")]) == 13


def test_include_incomplete_carries_partial_records_but_the_gate_still_rejects_them(tmp_path):
    root = tmp_path / "evidence"
    partial = _record(root, "hyprland", "realvnc-desktop", status="incomplete")
    result = evidence_store.publish(root, tmp_path / "store", COMMIT, include_incomplete=True)
    assert result.pairs == {("hyprland", "realvnc-desktop")}
    records_file = tmp_path / "store" / COMMIT / "records.json"
    published = json.loads(records_file.read_text(encoding="utf-8"))
    assert published[0]["run_id"] == partial["run_id"]
    errors = release_errors(published, COMMIT, tmp_path / "store" / COMMIT)
    assert not any("hyprland/realvnc-desktop" in error for error in errors)
    assert published[0]["status"] == "incomplete"


def test_an_artifact_altered_after_recording_stops_the_publication(tmp_path):
    root = tmp_path / "evidence"
    record = _record(root, "sway", "realvnc-desktop")
    (root / record["artifacts"][0]["path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="does not match its record"):
        evidence_store.publish(root, tmp_path / "store", COMMIT)
    assert not (tmp_path / "store" / COMMIT / "records.json").exists()


def test_an_artifact_path_outside_the_root_is_refused(tmp_path):
    root = tmp_path / "evidence"
    record = _record(root, "sway", "realvnc-desktop")
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    record["artifacts"][0]["path"] = "../secret.txt"
    record["artifacts"][0]["sha256"] = hashlib.sha256(b"secret").hexdigest()
    path = next((root / "records").glob("sway-*.json"))
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes the evidence root"):
        evidence_store.publish(root, tmp_path / "store", COMMIT, include_incomplete=True)


def test_publish_requires_a_commit_hash(tmp_path):
    for bad in ("", "../x", "a/b"):
        with pytest.raises(ValueError, match="full commit hash"):
            evidence_store.publish(tmp_path, tmp_path / "store", bad)
