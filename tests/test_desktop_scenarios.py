"""A fake driver exercises the scenario logic; it proves no viewer compatibility."""

import json
from pathlib import Path

import pytest
from PIL import Image

from wayland_vnc import desktop_scenarios as ds
from wayland_vnc.qualification import new_partial_record, validate_record

VERSIONS = {key: "x" for key in ("compositor", "backend", "viewer", "distribution", "renderer")}


def scene_image(path: Path, width: int, height: int, frame: int, markers=()) -> None:
    image = Image.new("RGB", (width, height), (0, 0, 0))
    colors = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))
    for index, color in enumerate(colors):
        image.paste(color, (index * width // 4, 0, (index + 1) * width // 4, height // 2))
    bands = {
        "keyboard": ((0, 255, 255), 0, height * 3 // 4),
        "pointer": ((255, 0, 255), width // 2, height * 3 // 4),
        "scroll": ((255, 255, 0), 0, height * 5 // 8),
        "drag": ((255, 170, 0), width // 2, height * 5 // 8),
    }
    for name in markers:
        color, x_pos, y_pos = bands[name]
        image.paste(color, (x_pos, y_pos, x_pos + width // 2, y_pos + height // 16))
    # A changing "frame counter" region below the targets.
    image.paste((frame % 256, 0, 0), (0, height - 1, 64, height))
    image.save(path)


class FakeDriver:
    """Deterministic stand-in: renders synthetic frames, counts calls, injects faults."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.mode = (1920, 1080, 1)
        self.frame = 0
        self.sessions = 0
        self.disconnects: list[bool] = []
        self.paused: list[float] = []
        self.restarts = 0
        self.smoke_calls: list[tuple[int, int]] = []
        self.smoke_ok = True
        self.blank_first = False
        self.freeze_after_pause = False
        self.die_on_pause = False
        self.modes: list[tuple[int, int, int]] = []
        self.dead: set[int] = set()
        self.transient_sessions: set[int] = set()
        self.drop_on_resize = False
        self.idle_ok = True
        self.idle_waits = 0
        self.mode_name = "approve"
        self.events: list[dict] = []
        self.restore_token = False
        self.remembered = False
        self.portal = False
        self.markers: set[str] = set()
        self.locked = False
        self.secret_typed = False
        self.outputs = 1
        self.hotplugs: list[bool] = []
        self.typed: list[str] = []
        self.clicks: list[tuple[int, int]] = []
        self.typed_focus = False
        self.suspends: list[float] = []
        self.suspend_transcript = {
            "before": {"status": "running", "running": True},
            "after_suspend": {"status": "suspended", "running": False},
            "after_wake": {"status": "running", "running": True},
            "verdict": "suspended and resumed",
        }
        self.die_on_suspend = False

    def suspend(self, seconds):
        self.suspends.append(seconds)
        if self.die_on_suspend:
            self.dead.update(range(1, self.sessions + 1))
        return dict(self.suspend_transcript)

    def smoke(self, width, height):
        self.smoke_calls.append((width, height))
        return self.smoke_ok and (width, height) == self.mode[:2]

    def crash_on_scale_down(self):
        """Emulate WayVNC dying on the 4K to 1080p restore."""
        original = self.set_mode

        def set_mode(width, height, scale):
            if self.mode == (3840, 2160, 2):
                self.smoke_ok = False
            original(width, height, scale)

        self.set_mode = set_mode

    def connect(self):
        self.sessions += 1
        if self.portal and not self.portal_decide():
            self.dead.add(self.sessions)
        return self.sessions

    def screenshot(self, session, path):
        if session in self.dead:
            return False
        if self.blank_first and self.frame == 0:
            self.frame += 1
            Image.new("RGB", (400, 200)).save(path)
            return True
        if not self.freeze_after_pause:
            self.frame += 1
        if self.locked:
            Image.new("RGB", (self.mode[0], self.mode[1]), (10, 10, 10)).save(path)
            return True
        scene_image(path, self.mode[0], self.mode[1], self.frame, self.markers)
        return True

    supports_input = False

    def type_text(self, text):
        self.typed.append(text)
        if self.typed_focus:
            self.markers.add("keyboard")
        if text == "\n" and self.secret_typed and self.locked:
            self.locked = False

    def click(self, x_pos, y_pos):
        self.clicks.append((x_pos, y_pos))
        if (x_pos, y_pos) == ds.SCENE_ENTRY:
            self.typed_focus = True
        if (x_pos, y_pos) == ds.SCENE_BUTTON:
            self.markers.add("pointer")

    def scroll(self, x_pos, y_pos):
        if y_pos < 500:
            self.markers.add("scroll")

    def drag(self, x_from, y_from, x_to, y_to):
        if abs(x_to - x_from) > 100 and y_from < 500:
            self.markers.add("drag")

    def type_secret(self):
        self.secret_typed = True
        if self.locked:  # a correct secret plus Return unlocks
            self.locked = False

    def lock(self):
        self.locked = True

    def force_unlock(self):
        self.locked = False

    def hotplug(self, attach):
        self.outputs = 2 if attach else 1
        self.hotplugs.append(attach)

    def disconnect(self, session, *, kill=False):
        self.disconnects.append(kill)
        self.dead.add(session)

    def alive(self, session):
        return session not in self.dead

    def transient_error(self, session):
        return session in self.transient_sessions

    def pause(self, seconds):
        self.paused.append(seconds)
        if self.die_on_pause:
            self.dead.add(self.sessions)

    def restart(self):
        self.restarts += 1
        self.remembered = False

    def set_mode(self, width, height, scale):
        self.mode = (width, height, scale)
        self.modes.append(self.mode)
        if self.drop_on_resize:
            self.dead.update(range(1, self.sessions + 1))

    def wait_idle(self):
        self.idle_waits += 1
        return self.idle_ok

    def portal_mode(self, mode):
        self.mode_name = mode

    def portal_events(self):
        return list(self.events)

    def portal_forget(self):
        self.restore_token = False

    def portal_decide(self):
        """Emulate the consent helper and KDE's per-lifetime memory of a decision."""
        if self.remembered or (self.mode_name == "none" and self.restore_token):
            return True
        if self.mode_name == "deny":
            self.events.append({"action": "Deny", "persist": False})
            return False
        if self.mode_name == "none":
            return False
        persist = self.mode_name == "approve-persist"
        self.events.append({"action": "Approve", "persist": persist})
        self.restore_token = self.restore_token or persist
        self.remembered = True
        return True


@pytest.fixture(name="lab")
def lab_fixture(tmp_path):
    evidence = tmp_path / "evidence" / "sway" / "run"
    evidence.mkdir(parents=True)
    driver = FakeDriver(tmp_path)
    ticks = iter(range(0, 100000))
    run = ds.Session(
        fixture="sway",
        evidence_dir=evidence,
        driver=driver,
        clock=lambda: next(ticks) * 0.1,
        sleep=lambda _seconds: None,
    )
    return run, driver, tmp_path / "evidence"


def test_healthy_run_passes_every_automated_scenario_but_stays_incomplete(lab):
    run, driver, root = lab
    outcomes = ds.run_scenarios(run)
    statuses = {name: outcome.status for name, outcome in outcomes.items()}
    for name in ds.AUTOMATED:
        assert statuses[name] == "passed", (name, outcomes[name].detail)
    assert statuses["colors"] == "passed"
    assert statuses["1080p-100"] == "passed"
    # Without an input-capable driver, input and lock stay not-run; sway still hot-plugs.
    for name in ds.MANUAL_SCENARIOS:
        expected = "passed" if name == "monitor-change" else "not-run"
        assert statuses[name] == expected, name
    assert "portal-approve" not in statuses
    assert run.reconnect_cycles == 20
    assert run.first_frame_seconds is not None
    assert driver.restarts == 1
    assert driver.paused == [ds.INTERRUPTION_SECONDS]
    assert driver.modes[-1] == (3840, 2160, 2)
    assert run.current_mode == (3840, 2160, 2)
    assert True in driver.disconnects  # viewer-killed used SIGKILL
    record = new_partial_record(
        commit="c", target="sway", viewer="realvnc-desktop", versions=VERSIONS
    )
    record = ds.fill_record(record, run, outcomes, root)
    assert record["status"] == "incomplete"
    assert record["post_run_smoke"] == "passed"
    assert record["scenarios"]["keyboard"] == "not-run"
    kinds = {item["kind"] for item in record["artifacts"]}
    # input-results is written even unattended, recording "not-run" with the reason.
    assert kinds == {"viewer-capture", "input-results", "runner-log"}
    errors = validate_record(record, "c", root)
    assert "missing, skipped, or failing scenarios" in errors
    assert "run did not pass" in errors
    assert not any("hash" in error or "escapes" in error for error in errors)
    assert (run.evidence_dir / "runner.log").read_text(encoding="utf-8").count("reconnect ") == 20


def test_plasma_adds_portal_scenarios_and_skips_mode_changes(lab):
    run, driver, root = lab
    run.fixture = "plasma"
    driver.portal = True
    outcomes = ds.run_scenarios(run)
    assert outcomes["resize"].status == "not-run"
    assert outcomes["4k-200"].status == "not-run"
    assert outcomes["portal-approve"].status == "passed", outcomes["portal-approve"].detail
    assert outcomes["portal-deny"].status == "passed", outcomes["portal-deny"].detail
    assert outcomes["portal-restore"].status == "passed", outcomes["portal-restore"].detail
    assert outcomes["portal-revoke"].status == "not-run"
    assert driver.mode_name == "approve"
    assert driver.restarts >= 5
    record = new_partial_record(
        commit="c", target="plasma", viewer="realvnc-desktop", versions=VERSIONS
    )
    record = ds.fill_record(record, run, outcomes, root)
    assert "portal-restore" in record["scenarios"]


def test_smoke_failure_fails_closed(lab):
    run, driver, _root = lab
    driver.smoke_ok = False
    outcomes = ds.run_scenarios(run)
    assert outcomes["first-frame"].status == "failed"
    assert outcomes["colors"].status == "failed"
    assert outcomes["1080p-100"].status == "failed"
    assert outcomes["server-restart"].status == "failed"
    assert outcomes["4k-200"].status == "failed"
    assert run.post_run_healthy is False


def test_first_frame_waits_past_an_invalid_capture(lab):
    run, driver, _root = lab
    driver.blank_first = True
    outcome = ds.scenario_first_frame(run)
    assert outcome.status == "passed"
    assert driver.frame >= 2


def test_first_frame_times_out_without_a_scene(lab):
    run, driver, _root = lab
    ticks = iter(range(0, 100000))
    run.clock = lambda: next(ticks) * 2.5
    driver.mode = (400, 200, 1)
    driver.screenshot = lambda _session, path: Image.new("RGB", (400, 200)).save(path) or True
    outcome = ds.scenario_first_frame(run)
    assert outcome.status == "failed"
    assert "no valid frame" in outcome.detail


def test_interruption_recovers_through_reconnect_when_the_session_died(lab):
    run, driver, _root = lab
    driver.die_on_pause = True
    outcome = ds.scenario_network_interruption(run)
    assert outcome.status == "passed"
    assert "reconnect" in outcome.detail


def test_interruption_with_frozen_frames_fails_when_reconnect_fails(lab):
    run, driver, _root = lab
    driver.freeze_after_pause = True
    original_connect = driver.connect

    def connect_then_die():
        session = original_connect()
        driver.dead.add(session)
        return session

    outcome_before = ds.scenario_network_interruption(run)
    assert outcome_before.status == "passed"  # reconnect path still worked
    driver.connect = connect_then_die
    outcome = ds.scenario_network_interruption(run)
    assert outcome.status == "failed"


def test_changing_frames_detects_a_static_framebuffer(lab):
    run, driver, _root = lab
    driver.freeze_after_pause = True
    outcome = ds.scenario_changing_frames(run)
    assert outcome.status == "failed"
    assert "did not change" in outcome.detail


def test_mode_scenarios_check_the_viewer_framebuffer_size(lab):
    run, driver, _root = lab
    driver.set_mode = lambda width, height, scale: None  # compositor ignores the request
    outcome = ds.scenario_resize(run)
    assert outcome.status == "failed"
    assert "did not become" in outcome.detail
    outcome = ds.scenario_high_dpi(run)
    assert outcome.status == "failed"
    assert "rejected" in outcome.detail


def test_live_resize_fails_when_the_server_drops_the_session(lab):
    run, driver, _root = lab
    driver.drop_on_resize = True
    outcome = ds.scenario_resize(run)
    assert outcome.status == "failed"
    assert "dropped" in outcome.detail
    assert driver.modes[-1] == ds.BASE_MODE


def test_high_dpi_requires_an_idle_server(lab):
    run, driver, _root = lab
    driver.idle_ok = False
    outcome = ds.scenario_high_dpi(run)
    assert outcome.status == "failed"
    assert "idle" in outcome.detail
    assert driver.idle_waits == 1


def test_reconnect_stops_at_the_first_failed_cycle(lab):
    run, driver, _root = lab
    original = driver.connect

    def flaky():
        session = original()
        if session == 3:
            driver.dead.add(session)
        return session

    driver.connect = flaky
    outcome = ds.scenario_reconnect(run)
    assert outcome.status == "failed"
    assert "cycle 3" in outcome.detail
    assert run.reconnect_cycles == 0


def test_viewer_killed_and_restart_failures(lab):
    run, driver, _root = lab
    driver.smoke_ok = False
    assert ds.scenario_server_restart(run).status == "failed"
    driver.smoke_ok = True
    calls = []
    original = driver.connect

    def second_dead():
        session = original()
        calls.append(session)
        if len(calls) == 2:
            driver.dead.add(session)
        return session

    driver.connect = second_dead
    assert ds.scenario_viewer_killed(run).status == "failed"


def test_scenario_exceptions_become_failures(lab):
    run, driver, _root = lab

    def broken(_width, _height, _scale):
        raise OSError("docker exec failed")

    driver.set_mode = broken
    outcomes = ds.run_scenarios(run)
    assert outcomes["resize"].status == "failed"
    assert "OSError" in outcomes["resize"].detail


def test_early_capture_failures_in_each_scenario(lab):
    run, driver, _root = lab
    driver.screenshot = lambda _session, _path: False
    assert ds.scenario_changing_frames(run).status == "failed"
    assert ds.scenario_viewer_killed(run).status == "failed"
    assert ds.scenario_server_restart(run).status == "failed"
    assert ds.scenario_network_interruption(run).status == "failed"
    assert ds.scenario_resize(run).status == "failed"


def test_record_passes_only_when_every_scenario_passed(lab):
    run, _driver, root = lab
    outcomes = ds.run_scenarios(run)
    for name, outcome in outcomes.items():
        if outcome.status == "not-run":
            outcomes[name] = ds.Outcome("passed", "pretend manual evidence for the unit test")
    record = new_partial_record(
        commit="c", target="sway", viewer="realvnc-desktop", versions=VERSIONS
    )
    record = ds.fill_record(record, run, outcomes, root)
    assert record["status"] == "passed"
    # A fully passed, healthy, unattended run now carries every artifact kind the
    # validator requires, so it validates -- the gap this locked in was the defect.
    errors = validate_record(record, "c", root)
    assert errors == []
    run.post_run_healthy = False
    record = ds.fill_record(dict(record, status="incomplete"), run, outcomes, root)
    assert record["status"] == "incomplete"
    assert record["post_run_smoke"] == "failed"


def test_high_dpi_runs_last_and_health_is_checked_at_that_mode(lab):
    run, driver, _root = lab
    driver.crash_on_scale_down()
    outcomes = ds.run_scenarios(run)
    assert outcomes["4k-200"].status == "passed"
    assert run.post_run_healthy is True
    assert driver.smoke_calls[-1] == (3840, 2160)
    names = list(outcomes)
    assert names.index("keyboard") < names.index("resize") < names.index("4k-200")


def test_a_fixture_crash_after_the_scenarios_is_recorded(lab):
    run, driver, _root = lab
    calls = []
    original = driver.smoke

    def smoke(width, height):
        calls.append((width, height))
        return original(width, height) and len(calls) < 4

    driver.smoke = smoke
    ds.run_scenarios(run)
    assert run.post_run_healthy is False
    assert any("FAILED" in line for line in run.log)


def test_portal_scenarios_fail_closed(lab):
    run, driver, _root = lab
    run.fixture = "plasma"
    driver.portal = True
    # A helper that never denies means pixels arrive after "Deny": must fail.
    driver.portal_decide = lambda: driver.events.append({"action": "Approve"}) or True
    assert ds.scenario_portal_deny(run).status == "failed"
    # No consent event at all while frames arrive is also a failure for approve.
    driver.portal_decide = lambda: True
    outcome = ds.scenario_portal_approve(run)
    assert outcome.status == "failed"
    assert "without an approval" in outcome.detail
    # Persist never granted.
    driver.portal_decide = lambda: (
        driver.events.append({"action": "Approve", "persist": False}) or True
    )
    assert ds.scenario_portal_restore(run).status == "failed"
    # Deny that still blanks but recovery fails.
    calls = []

    def deny_then_dead():
        calls.append(1)
        if len(calls) == 1:
            driver.events.append({"action": "Deny"})
        driver.dead.add(driver.sessions)
        return False

    driver.portal_decide = deny_then_dead
    outcome = ds.scenario_portal_deny(run)
    assert outcome.status == "failed"
    assert "re-approving" in outcome.detail


def test_input_scenarios_run_only_with_an_input_capable_driver(lab):
    run, driver, _root = lab
    outcomes = ds.scenario_input(run)
    assert all(outcome.status == "not-run" for outcome in outcomes.values())
    driver.supports_input = True
    outcomes = ds.scenario_input(run)
    assert {name: o.status for name, o in outcomes.items()} == {
        "keyboard": "passed",
        "pointer": "passed",
        "scroll": "passed",
        "drag": "passed",
    }
    assert driver.typed == ["nonce-2026"]
    assert ds.SCENE_ENTRY in driver.clicks
    driver.markers.clear()
    driver.typed_focus = False
    driver.click = lambda x_pos, y_pos: None  # clicks never reach the scene
    ticks = iter(range(0, 100000))
    run.clock = lambda: next(ticks) * 2.0
    outcomes = ds.scenario_input(run)
    assert outcomes["keyboard"].status == "failed"
    assert outcomes["pointer"].status == "failed"
    assert outcomes["scroll"].status == "passed"
    driver.screenshot = lambda _session, _path: False
    outcomes = ds.scenario_input(run)
    assert all(o.status == "failed" for o in outcomes.values())


def test_full_run_with_input_driver_can_produce_a_passing_wlroots_record(lab):
    run, driver, root = lab
    driver.supports_input = True
    outcomes = ds.run_scenarios(run)
    statuses = {name: o.status for name, o in outcomes.items()}
    remaining = {name for name, status in statuses.items() if status != "passed"}
    assert remaining == {"suspend-resume"}, "a container driver cannot suspend"
    assert driver.hotplugs == [True, False]
    assert driver.secret_typed
    driver.supports_suspend = True
    driver.mode = ds.BASE_MODE  # the first run deliberately leaves the fixture at 4K
    run.current_mode = ds.BASE_MODE
    outcomes = ds.run_scenarios(run)
    assert all(o.status == "passed" for o in outcomes.values()), {
        n: o.detail for n, o in outcomes.items() if o.status != "passed"
    }
    assert driver.suspends == [ds.SUSPEND_SECONDS]


def test_suspend_resume_needs_a_machine_that_can_and_proves_it_did(lab):
    """Only a VM driver claims `supports_suspend`; the hypervisor transcript must show
    a real suspended state and a resume, and the desktop must reach the viewer again."""
    run, driver, _root = lab
    assert ds.scenario_suspend_resume(run).status == "not-run"
    driver.supports_suspend = True
    outcome = ds.scenario_suspend_resume(run)
    assert outcome.status == "passed"
    assert "same session" in outcome.detail
    assert (run.evidence_dir / "suspend-transcript.json").is_file()
    # The session died over S3: a clean reconnect still passes.
    driver.die_on_suspend = True
    outcome = ds.scenario_suspend_resume(run)
    assert outcome.status == "passed"
    assert "again after S3" in outcome.detail
    # A machine that never reported suspended is not a pass, whatever the frames say.
    driver.die_on_suspend = False
    driver.suspend_transcript["after_suspend"] = {"status": "running", "running": True}
    outcome = ds.scenario_suspend_resume(run)
    assert outcome.status == "failed"
    assert "did not suspend" in outcome.detail


def test_lock_fails_when_the_desktop_leaks_or_unlock_fails(lab):
    run, driver, _root = lab
    driver.supports_input = True
    driver.lock = lambda: None  # lock never engages: desktop stays visible
    outcome = ds.scenario_lock(run)
    assert outcome.status == "failed"
    assert "stayed visible" in outcome.detail
    driver.lock = lambda: setattr(driver, "locked", True)
    driver.type_secret = lambda: None  # wrong password: secret typed but stays locked
    ticks = iter(range(0, 100000))
    run.clock = lambda: next(ticks) * 2.0
    outcome = ds.scenario_lock(run)
    assert outcome.status == "failed"
    assert "did not restore" in outcome.detail
    run.fixture = "gnome"
    assert ds.scenario_lock(run).status == "not-run"


def test_lock_that_drops_the_session_is_not_a_lock(lab):
    """GNOME Shell ends remote sessions when it locks; the viewer's closed-connection
    message hides the desktop and, once dismissed, its last frame shows again. Neither
    may pass for locked or for unlocked."""
    run, driver, _root = lab
    driver.supports_input = True
    ended = set()
    driver.alive = lambda session: session not in ended
    driver.lock = lambda: (setattr(driver, "locked", True), ended.add(driver.sessions))
    outcome = ds.scenario_lock(run)
    assert outcome.status == "failed"
    assert "ended the session" in outcome.detail

    ended.clear()
    driver.lock = lambda: setattr(driver, "locked", True)

    def unlock_then_drop():
        driver.locked = False
        ended.add(driver.sessions)

    driver.type_secret = unlock_then_drop
    outcome = ds.scenario_lock(run)
    assert outcome.status == "failed"
    assert "did not restore" in outcome.detail


def test_monitor_change_requires_a_hotplug_capable_fixture(lab):
    run, driver, _root = lab
    assert ds.scenario_monitor_change(run).status == "passed"
    # Every wlroots fixture hot-plugs (sway creates an output, the others switch a
    # spare one); GNOME and Plasma containers cannot, and say so.
    for fixture in ("labwc", "xfce-labwc", "lxqt-labwc", "wayfire"):
        run.fixture = fixture
        assert ds.scenario_monitor_change(run).status == "passed", fixture
    run.fixture = "gnome"
    assert ds.scenario_monitor_change(run).status == "not-run"
    run.fixture = "sway"
    driver.freeze_after_pause = True
    outcome = ds.scenario_monitor_change(run)
    assert outcome.status == "failed"
    assert "stopped changing" in outcome.detail


def test_reconnect_retries_a_transient_handshake_glitch(lab):
    run, driver, _root = lab
    original = driver.connect

    def connect():
        session = original()
        # Fail session #3 once as a transient viewer handshake error.
        if session == 3 and 3 not in driver.seen_glitch:
            driver.seen_glitch.add(3)
            driver.dead.add(session)
            driver.transient_sessions.add(session)
        return session

    driver.seen_glitch = set()
    driver.connect = connect
    outcome = ds.scenario_reconnect(run)
    # One glitch is absorbed by the connection itself (every scenario's first
    # connection gets that bounded retry) and noted in the run log; the scenario still
    # counts twenty clean cycles.
    assert outcome.status == "passed"
    assert "20 clean reconnects" in outcome.detail
    assert any("transient viewer handshake glitch, one retry" in note for note in run.log)
    assert run.reconnect_cycles == 20


def test_a_transient_glitch_is_retried_once_on_any_first_connection(lab):
    run, driver, _root = lab
    original = driver.connect

    def connect():
        session = original()
        if session == 1:
            driver.dead.add(session)
            driver.transient_sessions.add(session)
        return session

    driver.connect = connect
    assert ds.scenario_first_frame(run).status == "passed"
    assert any("first-frame.png: transient viewer handshake glitch" in n for n in run.log)
    # A second glitch in a row is a failure, not a retry loop.
    driver.connect = original
    glitchy = iter([True, True])

    def connect_twice_bad():
        session = original()
        if next(glitchy, False):
            driver.dead.add(session)
            driver.transient_sessions.add(session)
        return session

    driver.connect = connect_twice_bad
    assert ds.scenario_changing_frames(run).status == "failed"


def test_reconnect_gives_up_after_too_many_transient_glitches(lab):
    run, driver, _root = lab
    original = driver.connect

    def connect():
        session = original()
        driver.dead.add(session)  # every attempt glitches
        driver.transient_sessions.add(session)
        return session

    driver.connect = connect
    outcome = ds.scenario_reconnect(run)
    assert outcome.status == "failed"
    assert run.reconnect_cycles == 0


def test_an_unattended_record_carries_an_input_results_artifact(tmp_path):
    """The validator requires viewer-capture, input-results AND runner-log; without
    this an unattended run could never validate however well it passed."""
    from wayland_vnc.qualification import new_partial_record, validate_artifacts

    record = new_partial_record(commit="abc", target="sway", viewer="realvnc-desktop", versions={})
    run_dir = tmp_path / "sway" / "realvnc-desktop" / record["run_id"]
    run_dir.mkdir(parents=True)
    capture = run_dir / "first-frame.png"
    capture.write_bytes(b"synthetic")
    outcomes = {name: ds.Outcome("passed", "ok", [capture]) for name in ds.SCENARIOS}
    outcomes["keyboard"] = ds.Outcome("not-run", "requires a person typing in the actual viewer")
    session = ds.Session(fixture="sway", evidence_dir=run_dir, driver=object())
    session.post_run_healthy = True
    record = ds.fill_record(record, session, outcomes, tmp_path)
    kinds = {a["kind"] for a in record["artifacts"]}
    assert kinds == {"viewer-capture", "input-results", "runner-log"}
    assert not validate_artifacts(record["artifacts"], tmp_path)
    written = json.loads((run_dir / "input-results.json").read_text())
    assert written["attended"] is False
    assert written["scenarios"]["keyboard"]["status"] == "not-run"


def test_scenario_filter_runs_only_the_named_scenarios(lab):
    """A targeted re-run (`--scenario`): only the named scenario and its derived rows
    run; the input group and every other scenario are skipped."""
    run, _driver, _root = lab
    run.only = frozenset({"first-frame"})
    outcomes = ds.run_scenarios(run)
    assert outcomes["first-frame"].status == "passed"
    # first-frame ran, so its derived colours/1080p come with it.
    assert "colors" in outcomes
    assert "1080p-100" in outcomes
    # A sibling automated scenario was skipped entirely (not run, not listed).
    assert "reconnect-20" not in outcomes
    assert "changing-frames" not in outcomes
    # The input group did not run; those scenarios only carry their not-run reason.
    for name in ds.INPUT_SCENARIOS:
        assert outcomes[name].status == "not-run"


def test_scenario_filter_without_first_frame_skips_the_derived_rows(lab):
    """When the filter excludes first-frame, the derived rows are not invented."""
    run, _driver, _root = lab
    run.only = frozenset({"changing-frames"})
    outcomes = ds.run_scenarios(run)
    assert outcomes["changing-frames"].status == "passed"
    assert "first-frame" not in outcomes
    assert "colors" not in outcomes
    assert "1080p-100" not in outcomes


def test_a_targeted_rerun_records_what_it_skipped_as_not_run(lab):
    """--scenario used to crash fill_record with a KeyError on the first scenario it
    had not run. The skipped ones are recorded as not-run, so the record cannot pass."""
    run, _driver, root = lab
    outcomes = {"colors": ds.Outcome("passed", "ok")}
    record = new_partial_record(
        commit="c", target="sway", viewer="realvnc-desktop", versions=VERSIONS
    )
    record = ds.fill_record(record, run, outcomes, root)
    assert record["status"] != "passed"
    assert record["scenarios"]["colors"] == "passed"
    assert record["scenarios"]["first-frame"] == "not-run"
    assert "targeted re-run" in record["scenario_details"]["first-frame"]


def test_high_dpi_asks_a_viewer_that_can_zoom_to_fit_before_capturing(lab):
    """A viewer showing the desktop 1:1 on a smaller screen has only a corner of a 4K
    desktop in view. One that can fit is asked to, and only for the 4K capture: the
    base-resolution scenarios must not pay for a pinch they do not need."""
    run, driver, _root = lab
    fitted = []
    driver.fit_desktop = fitted.append
    assert ds.scenario_high_dpi(run).status == "passed"
    assert len(fitted) == 1
    ds.scenario_first_frame(run)
    assert len(fitted) == 1
