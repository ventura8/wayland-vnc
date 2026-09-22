"""Actual-viewer scenario orchestration for one isolated desktop fixture.

The scenarios drive a real RealVNC Viewer against a real fixture through a
``Driver``; every verdict comes from pixels the viewer rendered, checked by
``image_evidence``. Scenarios that need a human, a portal decision, or hardware
the fixture cannot provide are recorded as ``not-run`` with a reason, never as
passed. The resulting record is always ``incomplete`` unless every scenario passed.
"""

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from wayland_vnc.image_evidence import detect_markers, verify_scene
from wayland_vnc.qualification import PORTAL_SCENARIOS, SCENARIOS, artifact_entry

# Every scenario that restarts the fixture reports the same refusal when the smoke
# checks do not come back; the wording is the contract the records are read against.
SMOKE_FAILED_AFTER_RESTART = "fixture did not pass smoke checks after restart"
FIRST_FRAME_BUDGET = 10.0
RECONNECT_CYCLES = 20
# The RealVNC RA2 handshake occasionally returns a bad-length RSA on a rapid
# reconnect; a bounded number of such transient viewer glitches may be retried,
# but a server that refuses or a missing frame with a healthy handshake still fails.
RECONNECT_TRANSIENT_RETRIES = 3
BASE_MODE = (1920, 1080, 1)
HIGH_DPI_MODE = (3840, 2160, 2)
INTERRUPTION_SECONDS = 5
# Fixtures whose compositor mode the runner can change (wlr-output-management, sway's
# and Hyprland's own IPC).
RESIZABLE_FIXTURES = ("sway", "labwc", "xfce-labwc", "lxqt-labwc", "wayfire", "hyprland")
MANUAL_SCENARIOS = {
    "keyboard": "requires a person typing in the actual viewer",
    "pointer": "requires a person clicking in the actual viewer",
    "drag": "requires a person dragging in the actual viewer",
    "scroll": "requires a person scrolling in the actual viewer",
    "monitor-change": "this fixture cannot hot-plug a second output",
    "lock": "this fixture has no session lock the runner can drive",
    "suspend-resume": "this fixture cannot suspend; only a virtual-machine fixture can",
}
# How long the machine stays in S3; long enough for the viewer's socket to time out.
SUSPEND_SECONDS = 20
# Fixtures whose compositor can add and remove an output at run time: sway and
# Hyprland create one on request; the other wlroots fixtures start with a spare
# headless output that is switched on and off through wlr-output-management.
HOTPLUG_FIXTURES = ("sway", "labwc", "xfce-labwc", "lxqt-labwc", "wayfire", "hyprland")
# Fixtures with a real session lock (ext-session-lock) the runner can engage and unlock.
LOCKABLE_FIXTURES = ("sway", "labwc", "xfce-labwc", "lxqt-labwc", "wayfire", "hyprland")
# Scene widget positions at 1920x1080: the GTK box places the 500 px drawing area on
# top, then the label, the entry and the button; input lands inside these widgets.
SCENE_AREA = (960, 250)
SCENE_ENTRY = (960, 572)
SCENE_BUTTON = (960, 626)
INPUT_SCENARIOS = ("keyboard", "pointer", "scroll", "drag")
PORTAL_BLOCKED = {
    "portal-revoke": "the fixture has no way to revoke a granted session from the desktop",
}


class Driver(Protocol):
    """Side effects the scenarios need; the real one shells out to docker and vncviewer."""

    # Capability flags, not methods: the scenarios read supports_input and
    # supports_suspend to decide whether a scenario can run at all, and set
    # live_resize around a mode change. Declaring them here keeps a driver that
    # omits one from satisfying the protocol silently.
    live_resize: bool
    supports_input: bool
    supports_suspend: bool

    def smoke(self, width: int, height: int) -> bool: ...
    def connect(self) -> object: ...
    def screenshot(self, session: object, path: Path) -> bool: ...
    def disconnect(self, session: object, *, kill: bool = False) -> None: ...
    def alive(self, session: object) -> bool: ...
    def pause(self, seconds: float) -> None: ...
    def restart(self) -> None: ...
    def set_mode(self, width: int, height: int, scale: int) -> None: ...
    def wait_idle(self) -> bool: ...
    def portal_mode(self, mode: str) -> None: ...
    def portal_events(self) -> list[dict]: ...
    def portal_forget(self) -> None: ...
    def transient_error(self, session: object) -> bool: ...
    def type_text(self, text: str) -> None: ...
    def type_secret(self) -> None: ...
    def lock(self) -> None: ...
    def force_unlock(self) -> None: ...
    def hotplug(self, attach: bool) -> None: ...
    def click(self, x_pos: int, y_pos: int) -> None: ...
    def scroll(self, x_pos: int, y_pos: int) -> None: ...
    def drag(self, x_from: int, y_from: int, x_to: int, y_to: int) -> None: ...
    def suspend(self, seconds: float) -> dict: ...


@dataclass
class Outcome:
    status: str
    detail: str
    captures: list[Path] = field(default_factory=list)


@dataclass
class Session:
    """State shared between scenarios for one fixture run."""

    fixture: str
    evidence_dir: Path
    driver: Driver
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    first_frame_seconds: float | None = None
    reconnect_cycles: int = 0
    post_run_healthy: bool | None = None
    current_mode: tuple[int, int, int] = BASE_MODE
    log: list[str] = field(default_factory=list)
    # When set, only these scenarios run (targeted re-runs); the rest stay absent.
    only: frozenset[str] | None = None

    def note(self, message: str) -> None:
        self.log.append(message)

    def capture_valid(self, session: object, name: str, deadline: float) -> Path | None:
        """Poll until the viewer yields a screenshot with the ordered RGBW scene."""
        path = self.evidence_dir / name
        while True:
            if self.driver.screenshot(session, path):
                try:
                    verify_scene(path)
                    return path
                except (ValueError, OSError):
                    pass
            if self.clock() >= deadline or not self.driver.alive(session):
                return None
            self.sleep(0.25)


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _dimensions(path: Path) -> tuple[int, int]:
    evidence = verify_scene(path)
    return evidence.width, evidence.height


def _framebuffer_size(run: Session, session: object, capture: Path) -> tuple[int, int]:
    """The size of the frames the viewer receives: what the viewer itself reports
    when it can (the Android app states its "Desktop size"; its screen capture is
    always the phone's screen), else the size of the capture, which for the desktop
    viewer is the remote framebuffer."""
    report = getattr(run.driver, "framebuffer_size", None)
    if report is not None:
        size = report(session)
        if size is None:
            # For such a viewer the capture never is the framebuffer, so judging by
            # it would compare the phone's screen with the requested mode.
            raise OSError("the viewer did not report its desktop size")
        return size
    return _dimensions(capture)


def connect_and_capture(run: Session, name: str) -> tuple[object, Path | None, float]:
    """Connect and wait for the first valid frame; one bounded retry when the viewer
    reports a transport-level transient (a connection dropped before any RFB byte, or
    the RA2 handshake glitch) -- noted in the run log, never silent. A server that
    refuses or a missing frame on a healthy handshake is not retried."""
    for attempt in (1, 2):
        started = run.clock()
        session = run.driver.connect()
        # A viewer whose connect() answers its own dialogs first (the Android app)
        # reports when it actually asked the server, as `started_at` on the same
        # clock; the first-frame budget is the server's to meet from that moment, not
        # from the automation's typing. Drivers without it are timed from the call.
        started = getattr(session, "started_at", None) or started
        capture = run.capture_valid(session, name, started + FIRST_FRAME_BUDGET)
        if capture is not None or attempt == 2 or not run.driver.transient_error(session):
            return session, capture, run.clock() - started
        run.driver.disconnect(session)
        run.note(f"{name}: transient viewer handshake glitch, one retry")
    raise AssertionError("unreachable")


def scenario_first_frame(run: Session) -> Outcome:
    session, capture, elapsed = connect_and_capture(run, "first-frame.png")
    run.driver.disconnect(session)
    if capture is None:
        return Outcome("failed", f"no valid frame within {FIRST_FRAME_BUDGET:.0f}s")
    run.first_frame_seconds = round(elapsed, 3)
    return Outcome("passed", f"first valid frame after {elapsed:.3f}s", [capture])


def scenario_changing_frames(run: Session) -> Outcome:
    session, first, _ = connect_and_capture(run, "changing-1.png")
    if first is None:
        run.driver.disconnect(session)
        return Outcome("failed", "no valid first capture")
    run.sleep(1)
    second = run.capture_valid(session, "changing-2.png", run.clock() + 5)
    run.driver.disconnect(session)
    if second is None:
        return Outcome("failed", "no valid second capture")
    if _digest(first) == _digest(second):
        return Outcome("failed", "frames did not change", [first, second])
    width, height = _dimensions(first)
    return Outcome("passed", f"{width}x{height} frames differ one second apart", [first, second])


def scenario_reconnect(run: Session) -> Outcome:
    captures = []
    retries = 0
    cycle = 0
    while cycle < RECONNECT_CYCLES:
        session, capture, elapsed = connect_and_capture(run, f"reconnect-{cycle + 1:02d}.png")
        transient = capture is None and run.driver.transient_error(session)
        run.driver.disconnect(session)
        if capture is None:
            if transient and retries < RECONNECT_TRANSIENT_RETRIES:
                retries += 1
                run.note(f"reconnect {cycle + 1}: transient viewer handshake glitch, retrying")
                continue
            detail = f"reconnect cycle {cycle + 1} produced no valid frame"
            return Outcome("failed", detail, captures)
        cycle += 1
        captures.append(capture)
        run.note(f"reconnect {cycle}: {elapsed:.3f}s")
    run.reconnect_cycles = RECONNECT_CYCLES
    suffix = f" ({retries} transient retries)" if retries else ""
    return Outcome("passed", f"{RECONNECT_CYCLES} clean reconnects{suffix}", captures)


def scenario_viewer_killed(run: Session) -> Outcome:
    session, capture, _ = connect_and_capture(run, "viewer-killed-before.png")
    if capture is None:
        run.driver.disconnect(session)
        return Outcome("failed", "no valid frame before killing the viewer")
    run.driver.disconnect(session, kill=True)
    session, after, _ = connect_and_capture(run, "viewer-killed-after.png")
    run.driver.disconnect(session)
    if after is None:
        return Outcome("failed", "no valid frame after a killed viewer", [capture])
    return Outcome("passed", "server accepted a new session after SIGKILL", [capture, after])


def scenario_network_interruption(run: Session) -> Outcome:
    session, capture, _ = connect_and_capture(run, "interruption-before.png")
    if capture is None:
        run.driver.disconnect(session)
        return Outcome("failed", "no valid frame before the interruption")
    run.driver.pause(INTERRUPTION_SECONDS)
    after = None
    if run.driver.alive(session):
        after = run.capture_valid(session, "interruption-after.png", run.clock() + 5)
        if after is not None and _digest(after) == _digest(capture):
            after = None
    run.driver.disconnect(session)
    if after is not None:
        return Outcome("passed", "frames resumed on the same session", [capture, after])
    session, reconnected, _ = connect_and_capture(run, "interruption-reconnect.png")
    run.driver.disconnect(session)
    if reconnected is None:
        return Outcome("failed", "no recovery after the interruption", [capture])
    return Outcome("passed", "recovered through a clean reconnect", [capture, reconnected])


def scenario_server_restart(run: Session) -> Outcome:
    run.driver.restart()
    if not run.driver.smoke(BASE_MODE[0], BASE_MODE[1]):
        return Outcome("failed", SMOKE_FAILED_AFTER_RESTART)
    session, capture, elapsed = connect_and_capture(run, "server-restart.png")
    run.driver.disconnect(session)
    if capture is None:
        return Outcome("failed", "no valid frame after the server restart")
    return Outcome("passed", f"valid frame {elapsed:.3f}s after restart", [capture])


def _can(run: Session, capability: str, fixtures: tuple[str, ...]) -> bool:
    """A capability the fixture has by name (the container fixtures), or one the
    driver's machine adds -- a KVM guest gives GNOME and Plasma real DRM outputs and
    their own lock screens, which their containers do not have."""
    return run.fixture in fixtures or capability in getattr(run.driver, "machine_capabilities", ())


def _wake_lock_screen(run: Session) -> None:
    """GNOME's and Plasma's lock screens show their password prompt only after a key
    press (the shield); the wlroots lockers take typing at once. A viewer whose unlock
    already begins with a warm-up key (the Android app's SPACE+DEL) wakes the shield
    itself, and a space typed here would land in its password field."""
    if run.fixture in ("gnome", "plasma") and not getattr(
        run.driver, "wakes_own_lock_screen", False
    ):
        run.driver.type_text(" ")
        run.sleep(2)


def _restore_mode(run: Session) -> None:
    run.driver.wait_idle()
    run.driver.set_mode(*BASE_MODE)


def scenario_resize(run: Session) -> Outcome:
    """A live resize: the connected viewer must follow the new size without dropping."""
    if not _can(run, "resize", RESIZABLE_FIXTURES):
        return Outcome("not-run", "runner cannot change this compositor's output mode")
    width, height, scale = (1280, 720, 1)
    session, before, _ = connect_and_capture(run, "resize-before.png")
    if before is None:
        run.driver.disconnect(session)
        return Outcome("failed", "no valid frame before the resize")
    size = None
    try:
        run.driver.live_resize = True
        run.driver.set_mode(width, height, scale)
        after = run.capture_valid(session, "resize-after.png", run.clock() + FIRST_FRAME_BUDGET)
        alive = run.driver.alive(session)
        # Asked while still connected: a viewer that reports its desktop size (the
        # Android app) can only do so for a live session.
        if after is not None and alive:
            size = _framebuffer_size(run, session, after)
    finally:
        run.driver.live_resize = False
        run.driver.disconnect(session)
        _restore_mode(run)
    if not alive:
        return Outcome("failed", "server dropped the session during the mode change", [before])
    if after is None:
        return Outcome("failed", f"no valid frame at {width}x{height} after the resize", [before])
    if size != (width, height):
        return Outcome("failed", f"viewer framebuffer did not become {width}x{height}", [before])
    return Outcome(
        "passed", f"live viewer followed the resize to {width}x{height}", [before, after]
    )


def scenario_high_dpi(run: Session) -> Outcome:
    """Render at 3840x2160 with scale 2 on an idle server, then connect fresh.

    This runs last and leaves the fixture at that mode: WayVNC 0.9.1 can crash on the
    scale-down restore, which would only obscure the verdicts gathered before it.
    """
    if not _can(run, "resize", RESIZABLE_FIXTURES):
        return Outcome("not-run", "runner cannot change this compositor's output mode")
    width, height, scale = HIGH_DPI_MODE
    if not run.driver.wait_idle():
        return Outcome("failed", "server never became idle before the mode change")
    run.driver.set_mode(width, height, scale)
    run.current_mode = HIGH_DPI_MODE
    if not run.driver.smoke(width, height):
        return Outcome("failed", f"fixture rejected {width}x{height}@{scale}")
    session, capture, _ = connect_and_capture(run, "4k-200.png")
    # Asked while still connected: a viewer that reports its desktop size can only
    # do so for a live session.
    size = _framebuffer_size(run, session, capture) if capture is not None else None
    run.driver.disconnect(session)
    if capture is None:
        return Outcome("failed", f"no valid frame at {width}x{height}@{scale}")
    if size != (width, height):
        return Outcome("failed", f"viewer framebuffer is not {width}x{height}", [capture])
    return Outcome("passed", f"viewer rendered {width}x{height} at scale {scale}", [capture])


def _events_since(run: Session, count: int) -> list[dict]:
    return run.driver.portal_events()[count:]


def _fresh_portal_state(run: Session, *, forget: bool = True) -> bool:
    """Drop the stored restore token and restart so the real dialog is raised again."""
    if forget:
        run.driver.portal_forget()
    run.driver.restart()
    return run.driver.smoke(BASE_MODE[0], BASE_MODE[1])


def scenario_portal_approve(run: Session) -> Outcome:
    """The real consent dialog is approved and the session then renders."""
    if not _fresh_portal_state(run):
        return Outcome("failed", SMOKE_FAILED_AFTER_RESTART)
    run.driver.portal_mode("approve")
    seen = len(run.driver.portal_events())
    session, capture, _ = connect_and_capture(run, "portal-approve.png")
    run.driver.disconnect(session)
    actions = [e.get("action") for e in _events_since(run, seen)]
    if capture is None:
        return Outcome("failed", f"no valid frame after consent actions {actions}")
    if "Approve" not in actions:
        return Outcome("failed", "frames arrived without an approval on the consent dialog")
    return Outcome("passed", "consent dialog approved, session rendered", [capture])


def scenario_portal_deny(run: Session) -> Outcome:
    """Denying the dialog must leave the viewer without any desktop pixels."""
    if not _fresh_portal_state(run):
        return Outcome("failed", SMOKE_FAILED_AFTER_RESTART)
    run.driver.portal_mode("deny")
    seen = len(run.driver.portal_events())
    try:
        session, capture, _ = connect_and_capture(run, "portal-deny.png")
        run.driver.disconnect(session)
        actions = [e.get("action") for e in _events_since(run, seen)]
    finally:
        run.driver.portal_mode("approve")
    if "Deny" not in actions:
        return Outcome("failed", f"the dialog was not denied (actions {actions})")
    if capture is not None:
        return Outcome("failed", "desktop pixels reached the viewer after Deny", [capture])
    if not _fresh_portal_state(run):
        return Outcome("failed", SMOKE_FAILED_AFTER_RESTART)
    session, recovered, _ = connect_and_capture(run, "portal-deny-recovery.png")
    run.driver.disconnect(session)
    if recovered is None:
        return Outcome("failed", "no valid frame after re-approving")
    return Outcome("passed", "Deny left the viewer blank; a later approval rendered", [recovered])


def scenario_portal_restore(run: Session) -> Outcome:
    """A persisted approval must survive a restart without raising the dialog."""
    if not _fresh_portal_state(run):
        return Outcome("failed", SMOKE_FAILED_AFTER_RESTART)
    run.driver.portal_mode("approve-persist")
    seen = len(run.driver.portal_events())
    try:
        session, capture, _ = connect_and_capture(run, "portal-restore-grant.png")
        run.driver.disconnect(session)
        granted = _events_since(run, seen)
        if capture is None or not any(e.get("persist") for e in granted):
            return Outcome("failed", "persistent approval was not granted")
        if not _fresh_portal_state(run, forget=False):
            return Outcome("failed", SMOKE_FAILED_AFTER_RESTART)
        run.driver.portal_mode("none")
        seen = len(run.driver.portal_events())
        session, restored, _ = connect_and_capture(run, "portal-restore.png")
        run.driver.disconnect(session)
        prompted = _events_since(run, seen)
    finally:
        run.driver.portal_mode("approve")
    if restored is None:
        detail = "restore token did not start a session without a dialog"
        return Outcome("failed", detail, [capture])
    if prompted:
        return Outcome("failed", "a dialog was still raised despite the restore token")
    return Outcome("passed", "session restored from the persisted token", [capture, restored])


def scenario_input(run: Session) -> dict[str, Outcome]:
    """Type, click, scroll and drag through the actual viewer; the scene must acknowledge."""
    if not getattr(run.driver, "supports_input", False):
        return {name: Outcome("not-run", MANUAL_SCENARIOS[name]) for name in INPUT_SCENARIOS}
    session, capture, _ = connect_and_capture(run, "input-before.png")
    if capture is None:
        run.driver.disconnect(session)
        return {name: Outcome("failed", "no valid frame before input") for name in INPUT_SCENARIOS}
    actions = {
        "keyboard": lambda: (run.driver.click(*SCENE_ENTRY), run.driver.type_text("nonce-2026")),
        "pointer": lambda: run.driver.click(*SCENE_BUTTON),
        "scroll": lambda: run.driver.scroll(*SCENE_AREA),
        "drag": lambda: run.driver.drag(
            SCENE_AREA[0] - 500, SCENE_AREA[1], SCENE_AREA[0] + 500, 250
        ),
    }
    outcomes = {}
    try:
        for name, action in actions.items():
            action()
            deadline = run.clock() + 5
            found = set()
            while name not in found and run.clock() < deadline:
                path = run.evidence_dir / f"input-{name}.png"
                if run.driver.screenshot(session, path):
                    found = detect_markers(path)
                run.sleep(0.25)
            if name in found:
                outcomes[name] = Outcome("passed", f"scene acknowledged {name}", [path])
            else:
                outcomes[name] = Outcome("failed", f"no {name} acknowledgement in the viewer")
    finally:
        run.driver.disconnect(session)
    return outcomes


def _scene_visible(run: Session, session: object, name: str, seconds: float) -> Path | None:
    return run.capture_valid(session, name, run.clock() + seconds)


def _scene_hidden(run: Session, session: object, name: str, seconds: float) -> Path | None:
    """Return a capture with no scene targets, or None if the scene stayed visible."""
    deadline = run.clock() + seconds
    path = run.evidence_dir / name
    while run.clock() < deadline:
        if run.driver.screenshot(session, path):
            try:
                verify_scene(path)
            except ValueError:
                # Decoded, and the scene is not in it: that is what "hidden" means.
                return path
            except OSError:
                # A truncated or half-written capture is not evidence that the
                # scene went away, so keep polling until the deadline instead.
                pass
        run.sleep(0.25)
    return None


def scenario_lock(run: Session) -> Outcome:
    """Lock the session: the viewer must lose the desktop, then unlock through the viewer."""
    if not _can(run, "lock", LOCKABLE_FIXTURES) or not getattr(run.driver, "supports_input", False):
        return Outcome("not-run", MANUAL_SCENARIOS["lock"])
    session, before, _ = connect_and_capture(run, "lock-before.png")
    if before is None:
        run.driver.disconnect(session)
        return Outcome("failed", "no valid frame before locking")
    try:
        run.driver.lock()
        hidden = _scene_hidden(run, session, "lock-locked.png", FIRST_FRAME_BUDGET)
        if hidden is None:
            return Outcome("failed", "desktop pixels stayed visible after locking", [before])
        _wake_lock_screen(run)
        run.driver.type_secret()
        restored = _scene_visible(run, session, "lock-unlocked.png", FIRST_FRAME_BUDGET)
    finally:
        run.driver.disconnect(session)
        # Never leave the fixture locked for later scenarios; this is cleanup, not a verdict.
        run.driver.force_unlock()
    if restored is None:
        return Outcome(
            "failed", "unlocking through the viewer did not restore the desktop", [hidden]
        )
    return Outcome(
        "passed", "lock hid the desktop; remote unlock restored it", [before, hidden, restored]
    )


def scenario_monitor_change(run: Session) -> Outcome:
    """Hot-plug a second output and remove it while the viewer stays connected."""
    if not _can(run, "hotplug", HOTPLUG_FIXTURES):
        return Outcome("not-run", MANUAL_SCENARIOS["monitor-change"])
    session, before, _ = connect_and_capture(run, "monitor-before.png")
    if before is None:
        run.driver.disconnect(session)
        return Outcome("failed", "no valid frame before the monitor change")
    try:
        run.driver.hotplug(True)
        run.sleep(1)
        with_second = _scene_visible(run, session, "monitor-added.png", FIRST_FRAME_BUDGET)
        run.driver.hotplug(False)
        run.sleep(1)
        after = _scene_visible(run, session, "monitor-removed.png", FIRST_FRAME_BUDGET)
        alive = run.driver.alive(session)
    finally:
        run.driver.disconnect(session)
    if not alive or with_second is None or after is None:
        return Outcome("failed", "viewer lost the desktop across the monitor change", [before])
    if _digest(before) == _digest(after):
        return Outcome("failed", "frames stopped changing across the monitor change", [before])
    detail = "viewer kept a live 1920x1080 desktop while an output was added and removed"
    return Outcome("passed", detail, [before, with_second, after])


def scenario_suspend_resume(run: Session) -> Outcome:
    """Put the machine the desktop runs on through ACPI S3 with the viewer connected.

    Only a virtual-machine fixture can do this; the driver says so with
    `supports_suspend`, and the hypervisor's own transcript is the evidence that the
    machine really was suspended -- a scenario that merely waited is not a pass. After
    the wake, the desktop must reach the viewer again: on the surviving session if it
    kept streaming, otherwise through a clean reconnect.
    """
    if not getattr(run.driver, "supports_suspend", False):
        return Outcome("not-run", MANUAL_SCENARIOS["suspend-resume"])
    session, before, _ = connect_and_capture(run, "suspend-before.png")
    if before is None:
        run.driver.disconnect(session)
        return Outcome("failed", "no valid frame before suspending")
    transcript = run.driver.suspend(SUSPEND_SECONDS)
    transcript_path = run.evidence_dir / "suspend-transcript.json"
    transcript_path.write_text(json.dumps(transcript, indent=2, sort_keys=True) + "\n")
    run.note(f"suspend-resume: {transcript.get('verdict')}")
    if transcript.get("after_suspend", {}).get("status") != "suspended" or not transcript.get(
        "after_wake", {}
    ).get("running"):
        run.driver.disconnect(session)
        return Outcome("failed", f"machine did not suspend and resume: {transcript}", [before])
    after = None
    if run.driver.alive(session):
        after = run.capture_valid(session, "suspend-after.png", run.clock() + FIRST_FRAME_BUDGET)
        if after is not None and _digest(after) == _digest(before):
            after = None
    run.driver.disconnect(session)
    if after is not None:
        return Outcome("passed", "frames resumed on the same session after S3", [before, after])
    session, reconnected, _ = connect_and_capture(run, "suspend-reconnect.png")
    run.driver.disconnect(session)
    if reconnected is None:
        return Outcome("failed", "no valid frame after the machine resumed", [before])
    return Outcome("passed", "desktop reached the viewer again after S3", [before, reconnected])


PORTAL = {
    "portal-approve": scenario_portal_approve,
    "portal-deny": scenario_portal_deny,
    "portal-restore": scenario_portal_restore,
}

AUTOMATED = {
    "first-frame": scenario_first_frame,
    "changing-frames": scenario_changing_frames,
    "reconnect-20": scenario_reconnect,
    "viewer-killed": scenario_viewer_killed,
    "network-interruption": scenario_network_interruption,
    "server-restart": scenario_server_restart,
}
SESSION_SCENARIOS = {
    "monitor-change": scenario_monitor_change,
}
MODE_SCENARIOS = {
    "resize": scenario_resize,
    "4k-200": scenario_high_dpi,
}
# Last of all: after ACPI S3 a virtio-gpu guest cannot change its mode again (the DRM
# device never reopens), so every mode scenario has to be done by then. The machine
# suspends at whatever mode the 4K scenario left, and the post-run smoke checks it there.
SUSPEND_SCENARIOS = {
    "suspend-resume": scenario_suspend_resume,
}
# The session lock runs right after the input scenarios, at the base resolution.
FINAL_SCENARIOS = {
    "lock": scenario_lock,
}


def _run_group(run: Session, scenarios: dict, outcomes: dict[str, Outcome]) -> None:
    for name, scenario in scenarios.items():
        if name in outcomes:
            continue
        if run.only is not None and name not in run.only:
            continue
        try:
            outcomes[name] = scenario(run)
        except (OSError, ValueError) as error:
            outcomes[name] = Outcome("failed", f"{type(error).__name__}: {error}")
        run.note(f"{name}: {outcomes[name].status} - {outcomes[name].detail}")


def _run_input(run: Session, outcomes: dict[str, Outcome]) -> None:
    if run.only is not None and not set(INPUT_SCENARIOS) & run.only:
        return
    try:
        outcomes.update(scenario_input(run))
    except (OSError, ValueError) as error:
        detail = f"{type(error).__name__}: {error}"
        outcomes.update({name: Outcome("failed", detail) for name in INPUT_SCENARIOS})
    for name in INPUT_SCENARIOS:
        run.note(f"{name}: {outcomes[name].status} - {outcomes[name].detail}")


def _derived(outcomes: dict[str, Outcome]) -> None:
    """Colors and 1080p derive from the first frame."""
    if "first-frame" not in outcomes:
        return
    first = outcomes["first-frame"]
    if first.status == "passed":
        width, height = _dimensions(first.captures[0])
        outcomes["colors"] = Outcome(
            "passed", "ordered RGBW targets in the first frame", first.captures
        )
        base = width == BASE_MODE[0] and height == BASE_MODE[1]
        outcomes["1080p-100"] = Outcome(
            "passed" if base else "failed", f"first frame is {width}x{height}"
        )
    else:
        outcomes["colors"] = Outcome("failed", "no valid first frame")
        outcomes["1080p-100"] = Outcome("failed", "no valid first frame")


def run_scenarios(run: Session) -> dict[str, Outcome]:
    """Run every scenario in order; mode changes go last because they can crash servers."""
    outcomes: dict[str, Outcome] = {}
    if not run.driver.smoke(BASE_MODE[0], BASE_MODE[1]):
        outcomes["first-frame"] = Outcome("failed", "fixture did not pass smoke checks")
    _run_group(run, AUTOMATED, outcomes)
    _derived(outcomes)
    _run_input(run, outcomes)
    # Lock runs at the base resolution before any mode change: a wlroots session stays
    # locked if the locker exits without authenticating, so a reliable 1080p unlock keeps
    # a stuck lock (a real failure) from being masked by a later mode-change crash.
    _run_group(run, FINAL_SCENARIOS, outcomes)
    _run_group(run, SESSION_SCENARIOS, outcomes)
    if run.fixture == "plasma":
        _run_group(run, PORTAL, outcomes)
        for name, reason in PORTAL_BLOCKED.items():
            outcomes[name] = Outcome("not-run", reason)
    _run_group(run, MODE_SCENARIOS, outcomes)
    _run_group(run, SUSPEND_SCENARIOS, outcomes)
    # Fill any scenario no group produced with its reason.
    for name, reason in MANUAL_SCENARIOS.items():
        outcomes.setdefault(name, Outcome("not-run", reason))
    # A fixture that crashed during the run is a defect even when every scenario passed.
    run.post_run_healthy = run.driver.smoke(run.current_mode[0], run.current_mode[1])
    run.note(f"post-run fixture health: {'passed' if run.post_run_healthy else 'FAILED'}")
    return outcomes


def fill_record(record: dict, run: Session, outcomes: dict[str, Outcome], root: Path) -> dict:
    """Complete a partial record; it becomes 'passed' only if nothing is missing."""
    required = list(SCENARIOS) + (list(PORTAL_SCENARIOS) if run.fixture == "plasma" else [])
    record["scenarios"] = {name: outcomes[name].status for name in required}
    record["scenario_details"] = {name: outcomes[name].detail for name in required}
    record["first_frame_seconds"] = run.first_frame_seconds
    record["reconnect_cycles"] = run.reconnect_cycles
    captures = []
    for outcome in outcomes.values():
        for path in outcome.captures:
            if path not in captures:
                captures.append(path)
    record["artifacts"] = [artifact_entry(path, root, "viewer-capture") for path in captures]
    log_path = run.evidence_dir / "runner.log"
    log_path.write_text("\n".join(run.log) + "\n", encoding="utf-8")
    record["artifacts"].append(artifact_entry(log_path, root, "runner-log"))
    # The validator requires an input-results artifact alongside the captures and the
    # log. An unattended run must therefore write what it actually did for each input
    # scenario -- including "not-run" with the reason -- or it could never validate,
    # however well it passed.
    results_path = run.evidence_dir / "input-results.json"
    results_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "attended": False,
                "scenarios": {
                    name: {"status": outcomes[name].status, "detail": outcomes[name].detail}
                    for name in INPUT_SCENARIOS
                    if name in outcomes
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    record["artifacts"].append(artifact_entry(results_path, root, "input-results"))
    record["post_run_smoke"] = "passed" if run.post_run_healthy else "failed"
    if run.post_run_healthy and all(s == "passed" for s in record["scenarios"].values()):
        record["status"] = "passed"
    return record
