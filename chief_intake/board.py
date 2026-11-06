"""Append and conservatively update the final section of a shared board."""
from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path

from .models import Extraction, Flags
from .render import sanitize_field


class BoardError(Exception):
    pass


def board_line(date: date, from_: str, owner: str, flags: Flags,
               extraction: Extraction, record_basename: str, fallback_owner: str,
               user_name: str = "the user") -> str:
    if any(c in record_basename for c in "[]\r\n"):
        raise BoardError("unsafe record link")
    e = extraction
    ask = " ".join(filter(None, [sanitize_field(flags.label), sanitize_field(e.title),
                                sanitize_field(e.sender), sanitize_field(e.amount_due)]))
    if e.due_date:
        ask += f", due {e.due_date.isoformat()}"
    if flags.act_by:
        ask += f", act by {flags.act_by.isoformat()}"
    ask += f": proposed: {sanitize_field(e.first_step)}"
    if owner == fallback_owner:
        ask = f"triage with {sanitize_field(user_name)}: {ask}"
    prefix = f"- {date.isoformat()} · {sanitize_field(from_, 40)} → {sanitize_field(owner, 40)} · "
    suffix = f" · [[{record_basename}]]"
    return _fit(prefix, ask, suffix)


def _fit(prefix: str, ask: str, suffix: str) -> str:
    room = max(0, 300 - len(prefix) - len(suffix))
    if len(ask) > room:
        ask = ask[:max(0, room - 1)].rstrip() + ("…" if room else "")
    return prefix + ask + suffix


def _updated(line: str) -> str:
    try:
        body, link = line.rsplit(" · ", 1)
        prefix_date, route, ask = body.split(" · ", 2)
    except ValueError as exc:
        raise BoardError("invalid board line") from exc
    if not ask.startswith("UPDATED: "):
        ask = "UPDATED: " + ask
    return _fit(f"{prefix_date} · {route} · ", ask, " · " + link)


def ensure_line(board_path: Path, section: str, wanted_line: str,
                record_basename: str, is_update: bool = False,
                previous_record_basename: str | None = None,
                previous_line: str | None = None) -> str:
    path = Path(board_path)
    if "\n" in wanted_line or "\r" in wanted_line:
        raise BoardError("board line must be one line")
    for attempt in range(2):
        try:
            with path.open("rb") as file:
                original = file.read()
                stat = os.fstat(file.fileno())
            text = original.decode("utf-8")
            lines = text.splitlines(keepends=True)
            headers = [i for i, line in enumerate(lines) if line.startswith("## ")]
            targets = [i for i in headers if lines[i].rstrip("\r\n") == section]
            if len(targets) != 1 or targets[0] != headers[-1]:
                raise BoardError("board section must exist and be the last ## section")
            start = targets[0] + 1
            appended = _updated(wanted_line) if is_update else wanted_line
            for line in lines[start:]:
                if line.rstrip("\r\n") in {wanted_line, appended}:
                    return "present"
            matches = [i for i in range(start, len(lines))
                       if previous_line is not None
                       and lines[i].rstrip("\r\n") == previous_line]
            if matches:
                # Optimistic guard; other board writers need not cooperate with a lock.
                current = path.read_bytes()
                now = path.stat()
                if (now.st_mtime_ns != stat.st_mtime_ns or now.st_ino != stat.st_ino
                        or hashlib.sha256(current).digest() != hashlib.sha256(original).digest()):
                    continue
                lines[matches[0]] = wanted_line + "\n"
                path.write_text("".join(lines), encoding="utf-8")
                return "replaced"
            payload = (("\n" if original and not original.endswith(b"\n") else "")
                       + appended + "\n").encode("utf-8")
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                if os.write(fd, payload) != len(payload):
                    raise BoardError("short board append")
            finally:
                os.close(fd)
            return "updated-appended" if is_update else "appended"
        except (OSError, UnicodeError) as exc:
            raise BoardError(str(exc)) from exc
    raise BoardError("board changed during both update attempts")
