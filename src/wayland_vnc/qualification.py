"""Fail-closed release evidence validation; only trusted runners produce records."""

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from wayland_vnc.probe import TARGETS

VIEWERS = ("realvnc-android", "realvnc-desktop")
SCENARIOS = (
    "first-frame",
    "colors",
    "changing-frames",
    "keyboard",
    "pointer",
    "drag",
    "scroll",
    "1080p-100",
    "4k-200",
    "reconnect-20",
    "viewer-killed",
    "network-interruption",
    "resize",
    "monitor-change",
    "server-restart",
    "lock",
    "suspend-resume",
)
PORTAL_SCENARIOS = ("portal-approve", "portal-deny", "portal-revoke", "portal-restore")
VERSION_FIELDS = ("compositor", "backend", "viewer", "distribution", "renderer")
SCHEMA_VERSION = 2


def artifact_entry(path: Path, evidence_root: Path, kind: str) -> dict:
    """Create a content-addressed entry for a regular, non-symlink evidence file."""
    root = evidence_root.resolve()
    if path.is_symlink():
        raise ValueError("Evidence artifacts may not be symlinks")
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("Evidence artifact must be a regular file inside the evidence root")
    with resolved.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "path": str(resolved.relative_to(root)),
        "sha256": digest,
        "size": resolved.stat().st_size,
        "kind": kind,
    }


def new_partial_record(*, commit: str, target: str, viewer: str, versions: dict) -> dict:
    """Create a deliberately non-passing record for incremental trusted-runner evidence."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "commit": commit,
        "target": target,
        "viewer": viewer,
        "session_type": "wayland",
        "status": "incomplete",
        "versions": versions,
        "scenarios": {},
        "first_frame_seconds": None,
        "reconnect_cycles": 0,
        "artifacts": [],
    }


def _validate_identity(record: dict) -> list[str]:
    errors = []
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported schema")
    try:
        uuid.UUID(record.get("run_id", ""))
    except (ValueError, TypeError, AttributeError):
        errors.append("invalid run identifier")
    try:
        recorded_at = datetime.fromisoformat(record.get("recorded_at", "").replace("Z", "+00:00"))
        if recorded_at.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        errors.append("invalid evidence timestamp")
    return errors


def _validate_run_state(record: dict, commit: str) -> list[str]:
    """What the run claims about itself: the right commit, a known target and viewer,
    a native Wayland session, a pass, and a fixture still healthy afterwards."""
    errors = []
    if record.get("commit") != commit:
        errors.append("evidence belongs to a different commit")
    if record.get("target") not in TARGETS or record.get("viewer") not in VIEWERS:
        errors.append("unknown target or viewer")
    if record.get("session_type") != "wayland":
        errors.append("server session is not native Wayland")
    if record.get("status") != "passed":
        errors.append("run did not pass")
    # A fixture that died during the run can never qualify, whatever the scenarios say.
    # `status` is set by the producer, so the health check is required in its own right
    # rather than trusted through it.
    if record.get("post_run_smoke") != "passed":
        errors.append("post-run health check missing or failed")
    return errors


def _validate_versions(record: dict) -> list[str]:
    """Every version field present and non-blank; a record that cannot say what it ran
    against proves nothing about what it ran against."""
    versions = record.get("versions", {})
    if not isinstance(versions, dict) or any(
        not isinstance(versions.get(key), str) or not versions[key].strip()
        for key in VERSION_FIELDS
    ):
        return ["missing exact environment versions"]
    return []


def _validate_scenarios(record: dict) -> list[str]:
    """Every scenario the target owes, passed. Plasma owes the portal ones as well."""
    required = set(SCENARIOS)
    if record.get("target") == "plasma":
        required.update(PORTAL_SCENARIOS)
    scenarios = record.get("scenarios", {})
    if not isinstance(scenarios, dict) or any(scenarios.get(key) != "passed" for key in required):
        return ["missing, skipped, or failing scenarios"]
    return []


def _validate_measurements(record: dict) -> list[str]:
    """The two numbers the suite must have measured. `bool` is rejected explicitly
    because it is an `int` in Python, and True would otherwise pass as a latency."""
    errors = []
    latency = record.get("first_frame_seconds")
    if isinstance(latency, bool) or not isinstance(latency, (int, float)) or not 0 <= latency <= 10:
        errors.append("first rendered frame exceeds the 10-second budget")
    cycles = record.get("reconnect_cycles")
    if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 20:
        errors.append("fewer than twenty reconnects")
    return errors


def validate_record(record: dict, commit: str, evidence_root: Path) -> list[str]:
    """Validate completeness and hashes, not the honesty of an untrusted producer."""
    return (
        _validate_identity(record)
        + _validate_run_state(record, commit)
        + _validate_versions(record)
        + _validate_scenarios(record)
        + _validate_measurements(record)
        + validate_artifacts(record.get("artifacts"), evidence_root)
    )


def _artifact_integrity(item: object, root: Path) -> tuple[list[str], bool]:
    """Integrity of one referenced artifact: (errors, located).

    `located` is False when the entry is malformed, escapes the evidence root or names
    no file -- an integrity failure in its own right, and one that leaves nothing to
    hash or to vouch for the kind the entry claims.
    """
    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
        return ["invalid artifact entry"], False
    path = (root / item["path"]).resolve()
    source_path = root / item["path"]
    if source_path.is_symlink() or not path.is_relative_to(root) or not path.is_file():
        return ["artifact escapes evidence directory or is missing"], False
    errors = []
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != item.get("sha256"):
        errors.append("artifact hash mismatch")
    if item.get("size") != path.stat().st_size:
        errors.append("artifact size mismatch")
    return errors, True


def artifact_integrity_errors(artifacts: object, evidence_root: Path) -> list[str]:
    """Whether every referenced artifact is what the record says it is.

    Only integrity: each entry well-formed, inside the evidence root, present, and
    matching its recorded hash and size. Which KINDS of artifact a record must carry
    is completeness, checked by validate_artifacts; an incomplete record legitimately
    fails that while its integrity must still hold.
    """
    if not isinstance(artifacts, list):
        return []
    root = evidence_root.resolve()
    errors = []
    for item in artifacts:
        errors += _artifact_integrity(item, root)[0]
    return errors


def validate_artifacts(artifacts: object, evidence_root: Path) -> list[str]:
    """Validate synthetic evidence references without allowing directory escapes."""
    if not isinstance(artifacts, list) or not artifacts:
        return ["missing synthetic viewer evidence"]
    root = evidence_root.resolve()
    errors = []
    kinds = set()
    for item in artifacts:
        found, located = _artifact_integrity(item, root)
        errors += found
        if located:
            kinds.add(item.get("kind"))
    if not {"viewer-capture", "input-results", "runner-log"} <= kinds:
        errors.append("missing viewer capture, input results, or runner log")
    return errors


def release_errors(records: list[dict], commit: str, evidence_root: Path) -> list[str]:
    errors = []
    seen = set()
    for record in records:
        key = (record.get("target"), record.get("viewer"))
        if key in seen:
            errors.append(f"duplicate qualification: {key}")
        seen.add(key)
        errors.extend(f"{key}: {error}" for error in validate_record(record, commit, evidence_root))
    for target in TARGETS:
        for viewer in VIEWERS:
            if (target, viewer) not in seen:
                errors.append(f"missing qualification: {target}/{viewer}")
    return errors
