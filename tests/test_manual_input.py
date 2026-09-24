"""Attended-evidence merging accepts only marker-verified screenshots."""

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from wayland_vnc.manual_input import merge_manual_input
from wayland_vnc.qualification import new_partial_record, validate_record

VERSIONS = {key: "x" for key in ("compositor", "backend", "viewer", "distribution", "renderer")}


def screenshot(path: Path, *, inputs: bool, gestures: bool) -> Path:
    image = Image.new("RGB", (800, 400), "black")
    draw = ImageDraw.Draw(image)
    for index, color in enumerate(("red", "lime", "blue", "white")):
        draw.rectangle((index * 200, 0, (index + 1) * 200 - 1, 150), fill=color)
    if inputs:
        draw.rectangle((0, 350, 399, 399), fill="cyan")
        draw.rectangle((400, 350, 799, 399), fill="magenta")
    if gestures:
        draw.rectangle((0, 290, 399, 339), fill=(255, 255, 0))
        draw.rectangle((400, 290, 799, 339), fill=(255, 170, 0))
    image.save(path)
    return path


@pytest.fixture(name="record")
def record_fixture(tmp_path):
    root = tmp_path / "evidence"
    record = new_partial_record(
        commit="c", target="sway", viewer="realvnc-desktop", versions=VERSIONS
    )
    run_dir = root / "sway" / "realvnc-desktop" / record["run_id"]
    run_dir.mkdir(parents=True)
    log = run_dir / "runner.log"
    log.write_text("ok\n", encoding="utf-8")
    from wayland_vnc.qualification import SCENARIOS, artifact_entry

    record["scenarios"] = {name: "passed" for name in SCENARIOS}
    for name in ("keyboard", "pointer", "scroll", "drag"):
        record["scenarios"][name] = "not-run"
    record["first_frame_seconds"] = 0.3
    record["reconnect_cycles"] = 20
    record["post_run_smoke"] = "passed"
    capture = run_dir / "first-frame.png"
    screenshot(capture, inputs=False, gestures=False)
    record["artifacts"] = [
        artifact_entry(capture, root, "viewer-capture"),
        artifact_entry(log, root, "runner-log"),
    ]
    return record, root


def test_merge_requires_markers(record, tmp_path):
    rec, root = record
    shot = screenshot(tmp_path / "s.png", inputs=False, gestures=False)
    with pytest.raises(ValueError, match="keyboard and pointer"):
        merge_manual_input(rec, shot, root, gestures=False)
    assert rec["scenarios"]["keyboard"] == "not-run"


def test_merge_marks_input_and_gesture_scenarios(record, tmp_path):
    rec, root = record
    shot = screenshot(tmp_path / "s.png", inputs=True, gestures=False)
    merged = merge_manual_input(rec, shot, root, gestures=False)
    assert merged["scenarios"]["keyboard"] == "passed"
    assert merged["scenarios"]["scroll"] == "not-run"
    assert merged["status"] == "incomplete"
    kinds = {a["kind"] for a in merged["artifacts"]}
    assert "input-results" in kinds
    shot = screenshot(tmp_path / "g.png", inputs=True, gestures=True)
    merged = merge_manual_input(merged, shot, root, gestures=True)
    assert merged["status"] == "passed"
    assert validate_record(merged, "c", root) == []
    assert sum(a["path"].endswith("manual-input.png") for a in merged["artifacts"]) == 1
    results = json.loads(
        (
            root / "sway" / "realvnc-desktop" / merged["run_id"] / "manual-input-results.json"
        ).read_text()
    )
    assert "gestures" in results


def test_merge_refuses_unhealthy_or_foreign_records(record, tmp_path):
    rec, root = record
    shot = screenshot(tmp_path / "s.png", inputs=True, gestures=True)
    rec["post_run_smoke"] = "failed"
    merged = merge_manual_input(rec, shot, root, gestures=True)
    assert merged["status"] == "incomplete"
    rec["status"] = "failed"
    with pytest.raises(ValueError, match="Only incomplete"):
        merge_manual_input(rec, shot, root, gestures=True)
    rec["status"] = "incomplete"
    rec["run_id"] = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(ValueError, match="run directory"):
        merge_manual_input(rec, shot, root, gestures=True)


@pytest.mark.parametrize("field", ["target", "viewer", "run_id"])
def test_merge_refuses_a_record_whose_path_escapes_the_evidence_root(record, tmp_path, field):
    """The run directory is built from record fields, so a crafted one could steer the
    screenshot and results writes outside the evidence tree. The containment check must
    come before those writes, not after them in artifact_entry."""
    rec, root = record
    outside = tmp_path / "outside"
    outside.mkdir()
    # ".." often enough to leave the root, landing on a directory that really exists.
    rec[field] = "../" * 6 + outside.name
    shot = screenshot(tmp_path / "shot.png", inputs=True, gestures=True)
    with pytest.raises(ValueError, match="escapes the evidence root"):
        merge_manual_input(rec, shot, root, gestures=True)
    assert not (outside / "manual-input.png").exists(), "nothing may be written outside"
    assert not (outside / "manual-input-results.json").exists()
