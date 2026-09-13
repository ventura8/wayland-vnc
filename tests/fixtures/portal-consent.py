"""Act on the genuine KDE portal consent dialog through accessibility.

This stands in for the tester on the real xdg-desktop-portal-kde "Remote
Control" dialog, performed through AT-SPI so a headless fixture can complete the
portal flow. It never fakes a portal or bypasses KWin: every w0vncserver session
start still raises the real dialog, and this helper only presses the button the
current mode asks for. The mode file in XDG_RUNTIME_DIR selects ``approve``
(default), ``approve-persist`` (tick "allow restoring" first), ``deny`` or
``none`` (touch nothing). Every action is appended as JSON to
``portal-consent.jsonl`` for the qualification runner.
"""

import json
import os
import sys
import time
from pathlib import Path

import pyatspi

RUNTIME = Path(os.environ["XDG_RUNTIME_DIR"])
MODE_FILE = RUNTIME / "portal-consent-mode"
EVENT_FILE = RUNTIME / "portal-consent.jsonl"
BUTTONS = {"approve": ("Approve", "Share", "Allow"), "deny": ("Deny", "Cancel")}
BUTTON_ROLES = ("button", "push button")
PERSIST_LABEL = "Allow restoring on future sessions"
POLL_SECONDS = 1.0


MODES = ("approve", "approve-persist", "deny", "none")


def mode():
    try:
        value = MODE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "approve"
    if value not in MODES:
        # Defaulting a typo to "approve" would press Approve for a run that asked for
        # deny, and record consent evidence for a decision nobody made.
        raise SystemExit(f"portal-consent: unknown mode {value!r}; expected one of {MODES}")
    return value


def find(node, predicate, depth=0):
    if depth > 30:
        return None
    try:
        if predicate(node):
            return node
        for index in range(node.childCount):
            child = node.getChildAtIndex(index)
            if child is not None:
                found = find(child, predicate, depth + 1)
                if found is not None:
                    return found
    except Exception:
        return None
    return None


def is_button(labels):
    return lambda node: node.getRoleName() in BUTTON_ROLES and node.name in labels


def is_persist_box(node):
    return node.getRoleName() == "check box" and node.name == PERSIST_LABEL


def record(action, persist, owner, label):
    event = {
        "time": time.time(),
        # The canonical decision the runner checks for; `label` is whatever this
        # dialog happened to call the button ("Share", "Allow", "Cancel").
        "action": action,
        "label": label,
        "persist": persist,
        "application": owner,
    }
    with EVENT_FILE.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(event) + "\n")
    print(
        f"portal-consent: pressed {label!r} as {action!r} (persist={persist}) in {owner!r}",
        flush=True,
    )


def act_once(current):
    if current == "none":
        return False
    labels = BUTTONS["deny" if current == "deny" else "approve"]
    desktop = pyatspi.Registry.getDesktop(0)
    for index in range(desktop.childCount):
        application = desktop.getChildAtIndex(index)
        if application is None or "portal" not in (application.name or "").lower():
            continue
        button = find(application, is_button(labels))
        if button is None:
            continue
        persist = False
        if current == "approve-persist":
            box = find(application, is_persist_box)
            if box is None:
                # The dialog may still be building. Never approve without the box:
                # recording persist because a checkbox was absent would be a lie.
                return False
            if not box.getState().contains(pyatspi.STATE_CHECKED):
                if box.queryAction().doAction(0) is False:
                    return False
            # Derive persist from what the dialog now reports, not from the box's
            # existence, so an unchecked box cannot be recorded as a persistent grant.
            persist = box.getState().contains(pyatspi.STATE_CHECKED)
            if not persist:
                return False
        label, owner = button.name, application.name
        # Record only after the button actually acted; an event written for a press
        # that never happened is fabricated consent evidence.
        if button.queryAction().doAction(0) is False:
            return False
        record("Deny" if current == "deny" else "Approve", persist, owner, label)
        return True
    return False


while True:
    try:
        if act_once(mode()):
            time.sleep(3)
    except Exception as error:
        print(f"portal-consent: transient AT-SPI error: {error!r}", file=sys.stderr, flush=True)
    time.sleep(POLL_SECONDS)
