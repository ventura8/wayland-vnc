"""scripts/ppa-upload.sh: how a signed source upload reaches the Launchpad PPA.

Launchpad's anonymous FTP accepted the first source upload of a release run and
refused the second with "550 internal server error", so wayland-vnc-grd stopped
reaching the PPA and the GitHub release never ran. The script uploads over SFTP with
the account's key and a pinned host key, and skips a version Launchpad already has so
a re-run finishes a half-done job. `curl` and `dput` are fakes on PATH here and HOME is
a scratch directory: nothing reaches Launchpad or the real ~/.ssh.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ppa-upload.sh"
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nstand-in\n-----END OPENSSH PRIVATE KEY-----"

FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$RECORD/curl.args"
printf '%s' "$FAKE_LP_JSON"
"""
FAKE_DPUT = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$RECORD/dput.args"
cat "$2" > "$RECORD/dput.cf"
"""


@pytest.fixture
def lab(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("curl", FAKE_CURL), ("dput", FAKE_DPUT)):
        tool = bin_dir / name
        tool.write_text(body, encoding="utf-8")
        tool.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    record = tmp_path / "record"
    record.mkdir()
    changes = tmp_path / "wayland-vnc-grd_1.0.3+1ppa1~resolute1_source.changes"
    changes.write_text(
        "Format: 1.8\nSource: wayland-vnc-grd\nVersion: 1.0.3+1ppa1~resolute1\n",
        encoding="utf-8",
    )
    return {"bin": bin_dir, "home": home, "record": record, "changes": changes, "tmp": tmp_path}


def _run(lab, entries, key=KEY, ppa="ppa:ventura8/wayland-vnc", changes=None, cwd=None):
    env = {
        "PATH": f"{lab['bin']}:/usr/bin:/bin",
        "HOME": str(lab["home"]),
        "RECORD": str(lab["record"]),
        "FAKE_LP_JSON": json.dumps({"entries": entries}),
        "PPA_NAME": ppa,
        "PPA_SSH_PRIVATE_KEY": key,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), str(changes or lab["changes"])],
        cwd=cwd or REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_version_launchpad_already_records_is_skipped_without_touching_ssh(lab):
    result = _run(lab, [{"source_package_version": "1.0.3+1ppa1~resolute1", "status": "Pending"}])
    assert result.returncode == 0, result.stderr
    assert "already in ppa:ventura8/wayland-vnc (Pending)" in result.stdout
    assert not (lab["record"] / "dput.args").exists()
    assert not (lab["home"] / ".ssh").exists()


def test_the_lookup_asks_for_exactly_this_source_and_version(lab):
    _run(lab, [{"source_package_version": "1.0.3+1ppa1~resolute1", "status": "Published"}])
    args = (lab["record"] / "curl.args").read_text(encoding="utf-8").splitlines()
    assert "https://api.launchpad.net/devel/~ventura8/+archive/ubuntu/wayland-vnc" in args
    for query in (
        "ws.op=getPublishedSources",
        "source_name=wayland-vnc-grd",
        "version=1.0.3+1ppa1~resolute1",
        "exact_match=true",
    ):
        assert query in args


def test_without_the_ssh_key_it_fails_naming_the_secret_and_never_uses_ftp(lab):
    result = _run(lab, [], key="")
    assert result.returncode == 1
    assert "PPA_SSH_PRIVATE_KEY" in result.stderr
    assert not (lab["record"] / "dput.args").exists()


def test_a_new_version_goes_up_over_sftp_as_the_ppa_owner(lab):
    result = _run(lab, [])
    assert result.returncode == 0, result.stderr
    args = (lab["record"] / "dput.args").read_text(encoding="utf-8").splitlines()
    assert args[0] == "-c"
    assert args[2:] == ["launchpad-sftp", str(lab["changes"])]
    config = (lab["record"] / "dput.cf").read_text(encoding="utf-8")
    assert "method = sftp" in config
    assert "fqdn = ppa.launchpad.net" in config
    assert "incoming = ~ventura8/wayland-vnc" in config
    assert "login = ventura8" in config
    assert "allow_unsigned_uploads = 0" in config


def test_ssh_offers_only_this_key_and_trusts_only_the_pinned_host_key(lab):
    _run(lab, [])
    ssh = lab["home"] / ".ssh"
    key = ssh / "wayland-vnc-ppa"
    assert key.read_text(encoding="utf-8") == KEY + "\n"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(ssh.stat().st_mode) == 0o700
    pinned = (REPO / "packaging" / "launchpad-known-hosts").read_text(encoding="utf-8")
    assert (ssh / "wayland-vnc-ppa-known-hosts").read_text(encoding="utf-8") == pinned
    config = (ssh / "config").read_text(encoding="utf-8")
    for line in (
        "Host ppa.launchpad.net",
        f"IdentityFile {key}",
        "IdentitiesOnly yes",
        "StrictHostKeyChecking yes",
        "BatchMode yes",
    ):
        assert line in config


def test_a_second_upload_in_the_same_job_does_not_repeat_the_ssh_host_block(lab):
    _run(lab, [])
    _run(lab, [])
    config = (lab["home"] / ".ssh" / "config").read_text(encoding="utf-8")
    assert config.count("Host ppa.launchpad.net") == 1


def test_a_path_relative_to_the_caller_still_names_the_upload(lab):
    result = _run(lab, [], changes=lab["changes"].name, cwd=lab["tmp"])
    assert result.returncode == 0, result.stderr
    args = (lab["record"] / "dput.args").read_text(encoding="utf-8").splitlines()
    assert args[-1] == str(lab["changes"])


@pytest.mark.parametrize("ppa", ["ventura8/wayland-vnc", "ppa:ventura8", "ppa:../x/y"])
def test_a_malformed_ppa_name_is_refused(lab, ppa):
    result = _run(lab, [], ppa=ppa)
    assert result.returncode == 2
    assert "ppa:OWNER/NAME" in result.stderr


def test_the_pinned_host_key_is_launchpads_rsa_key():
    result = subprocess.run(
        ["ssh-keygen", "-lf", str(REPO / "packaging" / "launchpad-known-hosts")],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert "SHA256:MGq+4hxD7RduVTcfwlwwboZnsgJC6SL/NltM8ye+gNg ppa.launchpad.net (RSA)" in (
        result.stdout
    )


def test_both_release_uploads_go_through_the_script_with_the_key():
    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert workflow.count('scripts/ppa-upload.sh "${changes[0]}"') == 2
    assert workflow.count("PPA_SSH_PRIVATE_KEY: ${{ secrets.PPA_SSH_PRIVATE_KEY }}") == 2
    assert 'dput "$PPA_NAME"' not in workflow
