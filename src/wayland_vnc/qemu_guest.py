"""Talk to a QEMU guest through its two Unix sockets: QMP and the guest agent.

The actual-viewer qualification needs one thing a container cannot do: suspend the
machine the desktop runs on and wake it again. A KVM guest can, and this module is
the whole of what the runner needs from it -- exec a command inside, pause and resume
the VM, and take it through a real ACPI S3 cycle observed from the hypervisor side.
Both protocols are line-oriented JSON; nothing here depends on the qemu Python
package, which the packaging containers do not carry.
"""

import base64
import json
import socket
import time
from dataclasses import dataclass
from pathlib import Path


class GuestError(OSError):
    """The guest, or QEMU, refused or never answered."""


def _request(path: Path, message: dict, *, timeout: float, greeting: bool) -> dict:
    """One JSON request over a fresh connection to a QEMU Unix socket.

    QMP expects `qmp_capabilities` after its greeting; the guest agent has no
    greeting. A fresh connection per request keeps the client stateless, which matters
    after a suspend: a half-sent RPC on a frozen guest must not poison later calls.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        reader = sock.makefile("r", encoding="utf-8")
        if greeting:
            reader.readline()
            sock.sendall(b'{"execute": "qmp_capabilities"}\n')
            reader.readline()
        sock.sendall(json.dumps(message).encode("utf-8") + b"\n")
        while True:
            line = reader.readline()
            if not line:
                raise GuestError(f"{path.name}: connection closed without a reply")
            reply = json.loads(line)
            if "event" in reply:
                continue  # asynchronous QMP events are not the answer to this request
            if "error" in reply:
                raise GuestError(f"{path.name}: {reply['error']}")
            return reply.get("return", {})


@dataclass
class ExecResult:
    exitcode: int
    stdout: str
    stderr: str


class QemuGuest:
    """A running QEMU machine with a guest agent, addressed by its socket directory."""

    def __init__(self, work: Path, *, qmp: str = "qmp.sock", qga: str = "qga.sock"):
        self.qmp_path = work / qmp
        self.qga_path = work / qga

    # -- QMP: the hypervisor's view -------------------------------------------------

    def qmp(self, command: str, arguments: dict | None = None, *, timeout: float = 10) -> dict:
        message: dict = {"execute": command}
        if arguments:
            message["arguments"] = arguments
        return _request(self.qmp_path, message, timeout=timeout, greeting=True)

    def status(self) -> dict:
        """`query-status`: {"status": "running"|"suspended"|"paused"..., "running": bool}."""
        return self.qmp("query-status")

    def pause(self, seconds: float) -> None:
        """Freeze the whole machine for `seconds`: a network interruption from the
        viewer's point of view, exactly what `docker pause` is for a container."""
        self.qmp("stop")
        try:
            time.sleep(seconds)
        finally:
            self.qmp("cont")

    # -- guest agent: inside the machine --------------------------------------------

    def qga(self, command: str, arguments: dict | None = None, *, timeout: float = 10) -> dict:
        message: dict = {"execute": command}
        if arguments:
            message["arguments"] = arguments
        return _request(self.qga_path, message, timeout=timeout, greeting=False)

    def ping(self, timeout: float = 5) -> bool:
        try:
            self.qga("guest-ping", timeout=timeout)
            return True
        except (OSError, json.JSONDecodeError):
            return False

    def wait_ready(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.ping():
                return True
            time.sleep(1)
        return False

    def exec(self, argv: list[str], *, timeout: float = 60, stdin: str | None = None) -> ExecResult:
        """Run `argv` inside the guest as the agent's user (root) and wait for it.

        Secrets travel over the socket as base64 stdin, never on the guest's argv,
        where any process could read them.
        """
        arguments: dict = {
            "path": argv[0],
            "arg": argv[1:],
            "capture-output": True,
        }
        if stdin is not None:
            arguments["input-data"] = base64.b64encode(stdin.encode("utf-8")).decode("ascii")
        pid = self.qga("guest-exec", arguments)["pid"]
        deadline = time.monotonic() + timeout
        while True:
            state = self.qga("guest-exec-status", {"pid": pid})
            if state.get("exited"):
                return ExecResult(
                    state.get("exitcode", -1),
                    base64.b64decode(state.get("out-data", "")).decode("utf-8", "replace"),
                    base64.b64decode(state.get("err-data", "")).decode("utf-8", "replace"),
                )
            if time.monotonic() >= deadline:
                raise GuestError(f"guest command {argv[0]} did not finish within {timeout}s")
            time.sleep(0.25)

    def shell(self, script: str, *, timeout: float = 60, stdin: str | None = None) -> ExecResult:
        return self.exec(["/bin/sh", "-c", script], timeout=timeout, stdin=stdin)

    # -- the reason this module exists ----------------------------------------------

    def suspend_and_wake(self, seconds: float, *, settle: float = 60) -> dict:
        """Take the guest through ACPI S3 and back; return the hypervisor's transcript.

        `guest-suspend-ram` never answers -- the guest freezes mid-RPC -- so its
        timeout is expected and swallowed. The verdict comes from QMP `query-status`:
        the machine must actually report `suspended`, and `running` again after
        `system_wakeup`; a guest that merely blinked is not a suspend. The transcript
        is evidence, so every observed state is kept, not just the verdict.
        """
        transcript = {"before": self.status()}
        try:
            self.qga("guest-suspend-ram", timeout=3)
        except (OSError, json.JSONDecodeError):
            pass
        transcript["after_suspend"] = self._await_status("suspended", settle)
        if transcript["after_suspend"].get("status") != "suspended":
            transcript["verdict"] = "the machine never reported suspended"
            return transcript
        time.sleep(seconds)
        self.qmp("system_wakeup")
        transcript["after_wake"] = self._await_status("running", settle)
        if not transcript["after_wake"].get("running"):
            transcript["verdict"] = "the machine did not resume"
            return transcript
        transcript["agent_back"] = self.wait_ready(settle)
        transcript["verdict"] = (
            "suspended and resumed"
            if transcript["agent_back"]
            else ("resumed, but the guest agent never answered again")
        )
        return transcript

    def _await_status(self, wanted: str, seconds: float) -> dict:
        deadline = time.monotonic() + seconds
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.status()
            if last.get("status") == wanted:
                return last
            time.sleep(0.5)
        return last
