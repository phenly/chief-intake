"""Fresh Notes checks through safely parameterized AppleScript."""
from __future__ import annotations

from datetime import datetime
from subprocess import CompletedProcess
from typing import Callable

Runner = Callable[[list[str]], CompletedProcess]


class NotesControlError(Exception):
    pass


# Build local ISO components explicitly, avoiding locale-specific date text.
# The parser also returns local naive datetimes, so these compare directly.
_STATE = '''on pad(n, width)
    set s to n as text
    repeat while (count s) < width
        set s to "0" & s
    end repeat
    return s
end pad
on run argv
    tell application "Notes"
        if not (exists note id (item 1 of argv)) then return "MISSING"
        set n to note id (item 1 of argv)
        set folderName to name of container of n
        set d to modification date of n
    end tell
    set stamp to my pad(year of d, 4) & "-" & my pad((month of d) as integer, 2) & "-" & my pad(day of d, 2) & "T" & my pad(hours of d, 2) & ":" & my pad(minutes of d, 2) & ":" & my pad(seconds of d, 2)
    return folderName & linefeed & stamp
end run'''
_DELETE = '''on run argv
    tell application "Notes" to delete note id (item 1 of argv)
end run'''
_COUNT = '''on run argv
    tell application "Notes"
        set matches to every folder whose name is (item 1 of argv)
        if (count matches) is not 1 then error "Folder missing or ambiguous"
        return count of notes of (item 1 of matches)
    end tell
end run'''


class AppleScriptNotesControl:
    def __init__(self, runner: Runner):
        self.runner = runner

    def _run(self, script: str, argument: str) -> str:
        result = self.runner(["osascript", "-e", script, "--", argument])
        if result.returncode:
            raise NotesControlError(f"Notes automation failed: {result.stderr}")
        return result.stdout.rstrip("\r\n")

    def current_state(self, note_id: str) -> tuple[str, datetime] | None:
        output = self._run(_STATE, note_id)
        if output == "MISSING":
            return None
        try:
            folder, stamp = output.rsplit("\n", 1)
            return folder, datetime.fromisoformat(stamp)
        except ValueError as exc:
            raise NotesControlError("Invalid Notes state response") from exc

    def delete(self, note_id: str) -> None:
        self._run(_DELETE, note_id)

    def folder_count(self, folder: str) -> int:
        try:
            count = int(self._run(_COUNT, folder))
            if count < 0:
                raise ValueError("negative count")
            return count
        except ValueError as exc:
            raise NotesControlError("Invalid Notes folder count") from exc


class FakeNotesControl:
    def __init__(self, notes: dict[str, tuple[str, datetime]] | None = None):
        self.notes = dict(notes or {})
        self.deleted: list[str] = []

    def current_state(self, note_id: str) -> tuple[str, datetime] | None:
        return self.notes.get(note_id)

    def delete(self, note_id: str) -> None:
        if note_id not in self.notes:
            raise NotesControlError("Note missing")
        del self.notes[note_id]
        self.deleted.append(note_id)

    def folder_count(self, folder: str) -> int:
        return sum(name == folder for name, _ in self.notes.values())
