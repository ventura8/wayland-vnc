"""The QMP / guest-agent client, against a fake QEMU on a Unix socket."""

import base64
import json
import socket
import threading
from pathlib import Path

import pytest

from wayland_vnc import qemu_guest


class FakeQemu:
    """Answers QMP (with a greeting) and guest-agent requests from a script."""

    def __init__(self, work: Path):
        self.work = work
        self.statuses = ["running", "running", "suspended", "suspended", "running"]
        self.log: list[dict] = []
        self.exec_states = {}
        self.threads = []
        self._serve(work / "qmp.sock", greeting=True)
        self._serve(work / "qga.sock", greeting=False)

    def _serve(self, path: Path, *, greeting: bool):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen()

        def loop():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                with conn:
                    reader = conn.makefile("r", encoding="utf-8")
                    if greeting:
                        conn.sendall(b'{"QMP": {"version": {}}}\n')
                        reader.readline()  # qmp_capabilities
                        conn.sendall(b'{"return": {}}\n')
                    line = reader.readline()
                    if not line:
                        continue
                    request = json.loads(line)
                    self.log.append(request)
                    for reply in self.answer(request):
                        conn.sendall(json.dumps(reply).encode() + b"\n")

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        self.threads.append((server, thread))

    def answer(self, request):
        command = request["execute"]
        args = request.get("arguments", {})
        if command == "query-status":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return [
                {"event": "NOISE"},
                {"return": {"status": status, "running": status == "running"}},
            ]
        if command in ("stop", "cont", "system_wakeup", "guest-ping"):
            return [{"return": {}}]
        if command == "guest-suspend-ram":
            return []  # the guest freezes mid-RPC: no answer at all
        if command == "guest-exec":
            pid = 100 + len(self.exec_states)
            stdin = base64.b64decode(args.get("input-data", "")).decode()
            out = " ".join([args["path"], *args.get("arg", [])]) + (f" <{stdin}" if stdin else "")
            self.exec_states[pid] = {
                "exited": True,
                "exitcode": 3,
                "out-data": base64.b64encode(out.encode()).decode(),
            }
            return [{"return": {"pid": pid}}]
        if command == "guest-exec-status":
            return [{"return": self.exec_states[args["pid"]]}]
        return [{"error": {"class": "CommandNotFound", "desc": command}}]


@pytest.fixture(name="qemu")
def qemu_fixture(tmp_path):
    fake = FakeQemu(tmp_path)
    yield fake
    for server, _thread in fake.threads:
        server.close()


def test_exec_runs_inside_the_guest_and_secrets_go_over_stdin_not_argv(tmp_path, qemu):
    guest = qemu_guest.QemuGuest(tmp_path)
    result = guest.exec(["/bin/true", "--flag"], stdin="s3cret")
    assert result.exitcode == 3 and result.stdout == "/bin/true --flag <s3cret"
    sent = next(r for r in qemu.log if r["execute"] == "guest-exec")["arguments"]
    assert "s3cret" not in json.dumps(sent["arg"]), "secrets never appear on the guest argv"
    assert base64.b64decode(sent["input-data"]) == b"s3cret"


def test_pause_stops_and_always_continues_the_machine(tmp_path, qemu, monkeypatch):
    monkeypatch.setattr(qemu_guest.time, "sleep", lambda _s: None)
    qemu_guest.QemuGuest(tmp_path).pause(5)
    assert [r["execute"] for r in qemu.log] == ["stop", "cont"]


def test_suspend_and_wake_records_the_hypervisor_observed_cycle(tmp_path, qemu, monkeypatch):
    monkeypatch.setattr(qemu_guest.time, "sleep", lambda _s: None)
    transcript = qemu_guest.QemuGuest(tmp_path).suspend_and_wake(20, settle=5)
    assert transcript["after_suspend"]["status"] == "suspended"
    assert transcript["after_wake"]["running"] and transcript["agent_back"]
    assert transcript["verdict"] == "suspended and resumed"
    commands = [r["execute"] for r in qemu.log]
    assert "guest-suspend-ram" in commands and "system_wakeup" in commands
    assert commands.index("guest-suspend-ram") < commands.index("system_wakeup")


def test_suspend_that_never_suspends_is_reported_not_papered_over(tmp_path, qemu, monkeypatch):
    monkeypatch.setattr(qemu_guest.time, "sleep", lambda _s: None)
    qemu.statuses = ["running"]
    transcript = qemu_guest.QemuGuest(tmp_path).suspend_and_wake(1, settle=0.1)
    assert transcript["verdict"] == "the machine never reported suspended"
    assert "system_wakeup" not in [r["execute"] for r in qemu.log], (
        "no wake for a machine that never slept"
    )


def test_errors_from_qemu_raise(tmp_path, qemu):
    with pytest.raises(qemu_guest.GuestError):
        qemu_guest.QemuGuest(tmp_path).qmp("no-such-command")


def test_a_missing_guest_is_reported_not_hung(tmp_path, monkeypatch):
    """No socket at all: ping is False, wait_ready gives up, and a request raises."""
    monkeypatch.setattr(qemu_guest.time, "sleep", lambda _s: None)
    guest = qemu_guest.QemuGuest(tmp_path / "nowhere")
    assert not guest.ping()
    assert not guest.wait_ready(0.01)
    with pytest.raises(OSError):
        guest.qmp("query-status")


def test_a_connection_closed_without_a_reply_raises(tmp_path):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(tmp_path / "qga.sock"))
    server.listen()

    def hang_up():
        conn, _ = server.accept()
        conn.makefile("r", encoding="utf-8").readline()  # take the request, answer nothing
        conn.close()

    threading.Thread(target=hang_up, daemon=True).start()
    with pytest.raises(qemu_guest.GuestError, match="closed without a reply"):
        qemu_guest.QemuGuest(tmp_path).qga("guest-ping")
    server.close()


def test_exec_that_never_finishes_times_out(tmp_path, qemu, monkeypatch):
    monkeypatch.setattr(qemu_guest.time, "sleep", lambda _s: None)
    original = qemu.answer

    def never_exits(request):
        if request["execute"] == "guest-exec-status":
            return [{"return": {"exited": False}}]
        return original(request)

    qemu.answer = never_exits
    with pytest.raises(qemu_guest.GuestError, match="did not finish"):
        qemu_guest.QemuGuest(tmp_path).shell("sleep forever", timeout=0.01)


def test_a_machine_that_suspends_but_never_resumes_is_reported(tmp_path, qemu, monkeypatch):
    monkeypatch.setattr(qemu_guest.time, "sleep", lambda _s: None)
    qemu.statuses = ["running", "suspended", "suspended"]
    transcript = qemu_guest.QemuGuest(tmp_path).suspend_and_wake(1, settle=0.05)
    assert transcript["verdict"] == "the machine did not resume"
    assert "agent_back" not in transcript
