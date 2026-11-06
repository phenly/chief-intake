"""Daily Markdown reports and an independent local run journal."""
from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from .render import sanitize_field

Category = Literal["overdue", "due_soon", "routed", "updated", "couldnt", "waiting",
                   "not_committed", "cleanup"]
CATEGORIES = (("overdue", "Overdue"), ("due_soon", "Due soon"), ("routed", "Routed"),
              ("updated", "Updated"), ("couldnt", "Couldn't process"), ("waiting", "Waiting"),
              ("not_committed", "Not committed"), ("cleanup", "Cleanup"))


@dataclass(frozen=True)
class ReportItem:
    category: Category
    text: str
    link: str | None = None


def render_run_section(run_time: datetime, items: list[ReportItem]) -> str:
    if not items:
        return ""
    unknown = {item.category for item in items} - dict(CATEGORIES).keys()
    if unknown:
        raise ValueError(f"unknown report categories: {sorted(unknown)}")
    counts = Counter(item.category for item in items)
    # A record may be listed under both urgency and routing; count it once.
    processed = {item.link or (item.category, index) for index, item in enumerate(items)
                 if item.category in {"overdue", "due_soon", "routed", "updated"}}
    count_line = [f"{len(processed)} processed"]
    for key, label in (("overdue", "overdue"), ("due_soon", "due soon"),
                       ("couldnt", "couldn't process"), ("waiting", "waiting"),
                       ("not_committed", "not committed"), ("cleanup", "cleanup")):
        if counts[key]:
            count_line.append(f"{counts[key]} {label}")
    lines = [f"## Run {run_time:%H:%M}", "", " · ".join(count_line)]
    for key, heading in CATEGORIES:
        group = [item for item in items if item.category == key]
        if not group:
            continue
        lines.extend(["", f"### {heading}", ""])
        for item in group:
            line = "- " + sanitize_field(item.text)
            if item.link:
                if any(c in item.link for c in "[]\r\n"):
                    raise ValueError("unsafe report link")
                line += f" · [[{item.link}]]"
            lines.append(line)
    return "\n".join(lines) + "\n"


def write_report(inbox_path: Path, run_date: date, section: str) -> Path | None:
    if not section.strip():
        return None
    inbox_path = Path(inbox_path)
    inbox_path.mkdir(parents=True, exist_ok=True)
    path = inbox_path / f"Intake Run {run_date.isoformat()}.md"
    # O_APPEND also makes consecutive runs robust against accidental overwrites.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        prefix = f"# Intake Run {run_date.isoformat()}\n\n" if os.fstat(fd).st_size == 0 else "\n"
        payload = (prefix + section.rstrip() + "\n").encode("utf-8")
        if os.write(fd, payload) != len(payload):
            raise OSError("short report write")
    finally:
        os.close(fd)
    return path


def write_last_run(state_dir: Path, outcome: dict) -> None:
    outcome = dict(outcome)
    outcome.setdefault("trigger", "terminal")
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(outcome, ensure_ascii=False, default=str) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".last-run-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        if outcome['trigger'] in ('launchd', 'preflight'):
            # Terminal successes must not mask a scheduled permission failure.
            scheduled = state_dir / 'last-scheduled-run.json'
            scheduled_temp = state_dir / '.scheduled-run.tmp'
            scheduled_temp.write_text(payload, encoding='utf-8')
            os.replace(scheduled_temp, scheduled)
        os.replace(temporary, state_dir / "last-run.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    fd = os.open(state_dir / "runs.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        encoded = payload.encode("utf-8")
        if os.write(fd, encoded) != len(encoded):
            raise OSError("short run log write")
    finally:
        os.close(fd)
