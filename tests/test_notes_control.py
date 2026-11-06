# Support the prescribed pytest entry point in an uninstalled checkout.
import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from datetime import datetime
from subprocess import CompletedProcess

import pytest

from chief_intake.notes_control import AppleScriptNotesControl, FakeNotesControl, NotesControlError


def runner(output="", code=0):
    calls = []

    def run(argv):
        calls.append(argv)
        return CompletedProcess(argv, code, output, "synthetic failure" if code else "")

    return run, calls


def test_current_state_safely_passes_id():
    run, calls = runner("intake\n2026-11-06T07:08:09\n")
    note_id = 'synthetic" & delete every note & "'
    assert AppleScriptNotesControl(run).current_state(note_id) == ("intake", datetime(2026, 11, 6, 7, 8, 9))
    assert calls[0][-2:] == ["--", note_id]
    assert note_id not in calls[0][2]
    assert "on run argv" in calls[0][2] and "year of d" in calls[0][2]


def test_missing_and_delete():
    run, calls = runner("MISSING")
    control = AppleScriptNotesControl(run)
    assert control.current_state("missing") is None
    control.delete("synthetic")
    assert "delete note id (item 1 of argv)" in calls[-1][2]
    assert calls[-1][-1] == "synthetic"


def test_count():
    run, calls = runner("3\n")
    folder = 'intake"\\sample'
    assert AppleScriptNotesControl(run).folder_count(folder) == 3
    assert calls[0][-1] == folder and folder not in calls[0][2]


@pytest.mark.parametrize("method,output", [("current_state", "bad response"), ("folder_count", "bad"), ("folder_count", "-1")])
def test_malformed(method, output):
    run, _ = runner(output)
    with pytest.raises(NotesControlError):
        getattr(AppleScriptNotesControl(run), method)("synthetic")


@pytest.mark.parametrize("method", ["current_state", "delete", "folder_count"])
def test_command_failure(method):
    run, _ = runner(code=1)
    with pytest.raises(NotesControlError, match="automation failed"):
        getattr(AppleScriptNotesControl(run), method)("synthetic")


def test_fake():
    now = datetime(2026, 11, 6)
    control = FakeNotesControl({"a": ("intake", now), "b": ("other", now)})
    assert control.folder_count("intake") == 1
    assert control.current_state("a") == ("intake", now)
    control.delete("a")
    assert control.current_state("a") is None and control.deleted == ["a"]
    with pytest.raises(NotesControlError):
        control.delete("missing")
