"""Safe Markdown records and per-version image expiry."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from .models import Extraction, Flags, IntakeNote


def sanitize_field(s: str | None, max_length: int = 1000) -> str:
    """Flatten untrusted text into a bounded, inert Markdown field."""
    text = re.sub(r"<[^>]*>", "", s or "")
    text = text.replace("![[", "").replace("[[", "").replace("]]", "")
    text = text.replace("![", "").translate(str.maketrans("", "", "<>[]"))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[#>\-*\s]+", "", text)
    # Backticks and emphasis delimiters must not escape the generated labels.
    text = text.replace("`", "").replace("*", "").replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()[:max_length].rstrip()


def safe_title(s: str) -> str:
    # Keep separators inside numbers (24.00, 1,875.30); drop all other punctuation.
    clean = re.sub(r"(?<=\d)[.,](?=\d)", lambda m: "\x00" if m.group() == "." else "\x01",
                   sanitize_field(s))
    text = "".join(c for c in clean if c.isalnum() or c in " -\x00\x01")
    text = text.replace("\x00", ".").replace("\x01", ",")
    return re.sub(r"\s+", " ", text).strip()[:60].rstrip() or "Untitled"


def record_basename(captured_date: date, title: str, existing_names: Iterable[str]) -> str:
    base = f"Intake {captured_date.isoformat()} {safe_title(title)}"
    names = {Path(str(n)).stem if str(n).endswith(".md") else str(n)
             for n in existing_names}
    candidate, n = base, 2
    while candidate in names:
        candidate = f"{base} {n}"
        n += 1
    return candidate


def image_name(basename: str, n: int, ext: str) -> str:
    if n < 1 or not re.fullmatch(r"\.?[A-Za-z0-9]+", ext):
        raise ValueError("image number must be positive and extension alphanumeric")
    if any(c in basename for c in "/\\\r\n[]"):
        raise ValueError("unsafe image basename")
    return f"{basename} {n}.{ext.lstrip('.')}"


def _yaml(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    text = str(value)
    if (not text or text.lower() in {"null", "true", "false"}
            or re.search(r'[:#\[\]{}\n\r"\'&*!|>%@`]', text)
            or text != text.strip() or re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text)):
        return json.dumps(text, ensure_ascii=False)
    return text


def render_record(note: IntakeNote, extraction: Extraction, flags: Flags,
                  images: list[tuple[str, int | None]], processed: date,
                  fingerprint: str, user_name: str, updated: date | None = None) -> str:
    e = extraction
    metadata = {
        "type": "intake", "source": "apple-notes", "note_id": note.note_id,
        "fingerprint": fingerprint, "captured": note.created.date().isoformat(),
        "processed": processed.isoformat(),
    }
    if updated is not None:
        metadata["updated"] = updated.isoformat()
    metadata.update(owner=sanitize_field(e.owner), kind=sanitize_field(e.kind),
                    due=e.due_date.isoformat() if e.due_date else None,
                    penalty=e.penalty_date.isoformat() if e.penalty_date else None,
                    act_by=flags.act_by.isoformat() if flags.act_by else None,
                    overdue=flags.overdue, images=[name for name, _ in images],
                    model=sanitize_field(e.model))
    title = sanitize_field(e.title) or "Untitled"
    if e.sender:
        title += f" — {sanitize_field(e.sender)}"
    lines = ["---", *(f"{k}: {_yaml(v)}" for k, v in metadata.items()), "---", "",
             f"# {title}", "", f"**What:** {sanitize_field(e.summary)}"]
    amounts = []
    if e.amount_due:
        amounts.append(f"**Amount due:** {sanitize_field(e.amount_due)}")
    if e.due_date:
        amounts.append(f"**Due:** {e.due_date.isoformat()}")
    else:
        amounts.append("no due date printed")
    if e.penalty_date:
        amounts.append(f"**Penalty:** {e.penalty_date.isoformat()}")
    if flags.act_by:
        amounts.append(f"**Act by:** {flags.act_by.isoformat()}")
    lines.append(" · ".join(amounts))
    refs = [f"**{label}:** {sanitize_field(value)}" for label, value in
            (("Reference", e.reference), ("Contact", e.contact)) if value]
    if refs:
        lines.append(" · ".join(refs))
    lines.append(f"**Proposed first step:** {sanitize_field(e.first_step)}")
    if e.note_hunch:
        lines.append(f"**{sanitize_field(user_name)}'s note (hunch, not fact):** "
                     f"{sanitize_field(e.note_hunch)}")
    for name, pages in images:
        if any(c in name for c in "[]\r\n"):
            raise ValueError("unsafe image name")
        lines.extend(["", f"![[{name}]]"])
        if pages is not None:
            lines.append(f"PDF: {pages} {'page' if pages == 1 else 'pages'}.")
    problems = [a.problem for a in note.attachments if a.problem]
    if not e.readable:
        problems.append(e.unreadable_reason or "unreadable")
    for problem in dict.fromkeys(problems):
        lines.extend(["", f"Not read: {sanitize_field(problem)}"])
    return "\n".join(lines) + "\n"


def parse_front_matter(text: str) -> dict:
    """Read the flat scalar/JSON-array YAML subset emitted here."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}
    result = {}
    for line in lines[1:]:
        if line == "---":
            return result
        key, sep, raw = line.partition(":")
        if not sep:
            raise ValueError("invalid front matter")
        value = raw.strip()
        try:
            result[key] = json.loads(value)
        except json.JSONDecodeError:
            result[key] = value
    raise ValueError("unterminated front matter")


def expire_images_in_record(text: str, names_to_expire: Iterable[str], on_date: date) -> str:
    names = set(names_to_expire)
    metadata = parse_front_matter(text)
    if not metadata:
        raise ValueError("record has no front matter")
    lines = text.splitlines(keepends=True)
    end = next(i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---")
    for i in range(1, end):
        if lines[i].startswith("images:"):
            ending = "\r\n" if lines[i].endswith("\r\n") else "\n"
            lines[i] = "images: " + _yaml([n for n in metadata.get("images", [])
                                          if n not in names]) + ending
    for i in range(end + 1, len(lines)):
        if lines[i].strip() in {f"![[{n}]]" for n in names}:
            ending = "\n" if lines[i].endswith("\n") else ""
            lines[i] = f"Image expired {on_date.isoformat()} (kept 365 days).{ending}"
    return "".join(lines)
