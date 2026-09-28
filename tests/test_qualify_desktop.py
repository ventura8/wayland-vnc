"""What the desktop viewer's harness types, and how.

The GNOME and Plasma lock scenarios wake the lock shield with a single space before
typing the password. The harness used to refuse anything that was not alphanumeric,
so on a KVM guest the lock scenario died on a ValueError, left the desktop locked, and
took the next scenario (monitor-change) down with it.
"""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qualify_desktop", REPO / "scripts" / "qualify-desktop.py"
)
qualify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualify)


class Recorder(qualify.HarnessViewerMixin):
    """The mixin with the harness injection captured instead of run."""

    def __init__(self):
        self.injected = []

    def _inject(self, *arguments):
        self.injected.append(arguments)


def test_a_space_is_sent_as_its_key_so_the_lock_shield_wakes():
    viewer = Recorder()
    viewer.type_text(" ")
    assert viewer.injected == [("key", "0x0020")]


def test_return_is_sent_as_its_key():
    viewer = Recorder()
    viewer.type_text("\n")
    assert viewer.injected == [("key", "0xff0d")]


def test_words_are_typed_whole():
    viewer = Recorder()
    viewer.type_text("nonce-2026")
    assert viewer.injected == [("type", "nonce-2026")]


@pytest.mark.parametrize("text", ["two words", "a;b", "$HOME", ""])
def test_anything_else_is_refused_before_it_reaches_a_shell(text):
    viewer = Recorder()
    with pytest.raises(ValueError):
        viewer.type_text(text)
    assert not viewer.injected
