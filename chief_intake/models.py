"""Shared data types and interfaces. Every module codes against these; change them only in the integration lane."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

# ---- Notes side -------------------------------------------------------------

ATTACHMENT_PHOTO = "photo"   # resolved media file (jpg/heic/png)
ATTACHMENT_SCAN = "scan"     # com.apple.paper.doc.scan -> FallbackPDF.pdf
ATTACHMENT_OTHER = "other"   # anything intake can't read -> note needs attention


@dataclass(frozen=True)
class Attachment:
    uuid: str
    kind: str                       # ATTACHMENT_PHOTO | ATTACHMENT_SCAN | ATTACHMENT_OTHER
    type_uti: str                   # raw type identifier, e.g. "public.jpeg", "com.apple.paper.doc.scan"
    path: Path | None               # resolved file; None = not synced yet (note is waiting)
    problem: str | None = None      # e.g. "ambiguous media", "scan older than note"


@dataclass(frozen=True)
class IntakeNote:
    note_id: str                    # AppleScript id, x-coredata://.../ICNote/p123
    account_uuid: str
    title: str
    text: str
    created: datetime
    modified: datetime
    attachments: tuple[Attachment, ...] = ()


class NotesSource(Protocol):
    def list_notes(self) -> list[IntakeNote]:
        """Notes in the configured folder only. Never reads other folders or Recently Deleted."""


class NotesControl(Protocol):
    """Live checks and deletion through Notes itself (AppleScript). Fake in tests."""

    def current_state(self, note_id: str) -> tuple[str, datetime] | None:
        """(folder name, modification date) fresh from Notes, or None if the note no longer exists."""

    def delete(self, note_id: str) -> None:
        """Move the note to Recently Deleted. Raises on failure."""

    def folder_count(self, folder: str) -> int:
        """Preflight: number of notes in the folder (proves Automation permission)."""


# ---- Extraction ---------------------------------------------------------------

MAIL_KINDS = ("bill", "statement", "notice", "tax", "insurance")
KINDS = MAIL_KINDS + ("idea", "request", "other")


@dataclass(frozen=True)
class MediaPart:
    path: Path                      # prepared file in the run work dir (jpg or pdf)
    media_type: str                 # "image/jpeg" | "application/pdf"


@dataclass(frozen=True)
class Extraction:
    readable: bool
    kind: str
    title: str
    summary: str
    owner: str
    first_step: str
    unreadable_reason: str | None = None
    sender: str | None = None
    amount_due: str | None = None
    due_date: date | None = None
    penalty_date: date | None = None
    reference: str | None = None
    contact: str | None = None
    note_hunch: str | None = None
    model: str = ""


class ExtractionError(Exception):
    """A failed attempt: CLI exit, timeout, missing result, schema or date violation."""


class Extractor(Protocol):
    def extract(self, note: IntakeNote, media: list[MediaPart], today: date, model: str) -> Extraction:
        """One call carrying every media part plus the note text. Raises ExtractionError."""


# ---- Dates -------------------------------------------------------------------


@dataclass(frozen=True)
class Flags:
    overdue: bool
    due_soon: bool
    act_by: date | None
    label: str                      # e.g. "OVERDUE (due 2026-11-02; penalty 2026-11-30)" or "" ; used on board/report


# ---- State -------------------------------------------------------------------

# status values
PENDING = "pending"
WAITING = "waiting"
IN_PROGRESS = "in_progress"
DONE = "done"
NEEDS_ATTENTION = "needs_attention"
SUPERSEDED = "superseded"
DELETED = "deleted"


@dataclass
class NoteRow:
    note_id: str
    fingerprint: str
    note_modified: datetime
    status: str = PENDING
    attempts: int = 0
    last_error: str | None = None
    title: str = ""
    extraction_json: str | None = None      # serialized Extraction, kept so stages can resume without re-calling the model
    record_path: str | None = None          # vault-relative
    record_sha256: str | None = None
    image_paths: list[str] = field(default_factory=list)    # vault-relative
    image_sha256: list[str] = field(default_factory=list)
    owner: str | None = None
    board_line: str | None = None
    stage_record_at: datetime | None = None
    stage_board_at: datetime | None = None
    stage_report_at: datetime | None = None
    stage_commit_at: datetime | None = None
    completed_at: datetime | None = None
    deleted_at: datetime | None = None
    images_expired_at: datetime | None = None
    first_seen_at: datetime | None = None
