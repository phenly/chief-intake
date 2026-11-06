"""Folder-limited Notes reader and synthetic JSONL source."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from apple_notes_parser.database import AppleNotesDatabase

from .config import Config
from .models import Attachment, IntakeNote, ATTACHMENT_OTHER, ATTACHMENT_PHOTO, ATTACHMENT_SCAN


class NotesSourceError(Exception):
    """The configured source cannot be read unambiguously."""


# These inline text annotations are already represented in the note text. File
# links, tables, drawings, audio and all unknown UTIs remain unsupported content.
INLINE_UTIS = {f"com.apple.notes.inlinetextattachment.{s}" for s in ("hashtag", "mention", "link")}
PHOTO_UTIS = {"public.jpeg", "public.png", "public.heic", "public.heif"}
SCAN_UTI = "com.apple.paper.doc.scan"


def _identity(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and "/" not in value and "\\" not in value


def resolve_attachment(container: Path, account_uuid: str, uuid: str,
                       type_uti: str, modified: datetime,
                       media_id: str | None = None, media_filename: str | None = None) -> Attachment:
    """Resolve only exact identities; never search filenames across notes.

    Photos live under Media/<media id>/<generation>/<filename>, where the media id is the
    ICMedia row the attachment points at (ZMEDIA), not the attachment's own identifier.
    """
    kind = (ATTACHMENT_PHOTO if type_uti in PHOTO_UTIS else
            ATTACHMENT_SCAN if type_uti == SCAN_UTI else ATTACHMENT_OTHER)
    if kind == ATTACHMENT_OTHER or not (_identity(account_uuid) and _identity(uuid)):
        return Attachment(uuid, kind, type_uti, None)
    account = container / "Accounts" / account_uuid
    if kind == ATTACHMENT_PHOTO and not (media_id and _identity(media_id) and media_filename):
        return Attachment(uuid, kind, type_uti, None)  # media row not synced yet
    root = account / ("Media" if kind == ATTACHMENT_PHOTO else "FallbackPDFs") / (
        media_id if kind == ATTACHMENT_PHOTO else uuid)
    pattern = "**/*" if kind == ATTACHMENT_PHOTO else "*/FallbackPDF.pdf"
    candidates = [p for p in root.glob(pattern) if p.is_file()
                  and not any(s.startswith(".") for s in p.relative_to(root).parts)
                  and p.resolve().is_relative_to(root.resolve())]
    if not candidates:
        return Attachment(uuid, kind, type_uti, None)
    if kind == ATTACHMENT_PHOTO:
        candidates = [p for p in candidates if p.name == media_filename]
        if not candidates:
            return Attachment(uuid, kind, type_uti, None)
        if len(candidates) != 1:
            return Attachment(uuid, kind, type_uti, None, "ambiguous media")
        return Attachment(uuid, kind, type_uti, candidates[0])
    path = max(candidates, key=lambda p: (p.stat().st_mtime, str(p)))
    if path.stat().st_mtime < modified.timestamp():
        return Attachment(uuid, kind, type_uti, None, "scan older than note")
    with path.open("rb") as stream:
        header = stream.read(4)
    # Lightweight validation; compressed PDF object streams are not supported by
    # the page counter. Invalid or uncountable scans must never be processed.
    from .media import pdf_page_count
    if header != b"%PDF" or pdf_page_count(path) < 1:
        return Attachment(uuid, kind, type_uti, None, "invalid scan PDF")
    return Attachment(uuid, kind, type_uti, path)


def _media_row(db, attachment_uuid: str) -> tuple[str | None, str | None]:
    """(media identifier, filename) of the ICMedia row an attachment points at, read-only."""
    row = db.connection.execute(
        "SELECT m.ZIDENTIFIER, m.ZFILENAME FROM ZICCLOUDSYNCINGOBJECT a "
        "JOIN ZICCLOUDSYNCINGOBJECT m ON m.Z_PK = a.ZMEDIA WHERE a.ZIDENTIFIER = ?",
        (attachment_uuid,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


class ParserSource:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def list_notes(self) -> list[IntakeNote]:
        if self.cfg.notes_folder.casefold() == "recently deleted":
            raise NotesSourceError("Recently Deleted cannot be an intake folder")
        with AppleNotesDatabase(str(self.cfg.notes_container / "NoteStore.sqlite")) as db:
            accounts = {a.id: a for a in db.get_accounts()
                        if not self.cfg.notes_account or self.cfg.notes_account in (a.name, a.identifier)}
            folders = [f for f in db.get_folders(accounts) if f.name == self.cfg.notes_folder]
            if len(folders) != 1:
                reason = "not found" if not folders else "ambiguous; set notes_account"
                raise NotesSourceError(f"Notes folder {self.cfg.notes_folder!r}: {reason}")
            folder = folders[0]
            # Installed get_notes fetches all rows but checks account_id/folder_id
            # BEFORE ProtobufParser decoding (and embedded-object decoding).
            # Use that path, with exactly one folder; regression test pins it.
            notes = db.get_notes({folder.account.id: folder.account}, {folder.id: folder})
            result = []
            for note in notes:
                if not note.applescript_id or not note.creation_date or not note.modification_date:
                    raise NotesSourceError("Note missing AppleScript ID or timestamps")
                if note.is_password_protected:
                    # Reported as needs-attention by the pipeline, never silently skipped.
                    attachments = (Attachment("locked", ATTACHMENT_OTHER, "locked-note", None,
                                              "password-protected note"),)
                else:
                    attachments = tuple(resolve_attachment(
                        self.cfg.notes_container, note.account.identifier, a.uuid or "",
                        a.type_uti or "", note.modification_date,
                        *_media_row(db, a.uuid or ""))
                        for a in note.attachments if a.type_uti not in INLINE_UTIS)
                result.append(IntakeNote(note.applescript_id, note.account.identifier,
                                         note.title or "", note.content or "", note.creation_date,
                                         note.modification_date, attachments))
            return result


class FixtureSource:
    def __init__(self, jsonl_path: str | Path, files_dir: str | Path):
        self.jsonl_path = Path(jsonl_path)
        self.files_dir = Path(files_dir)

    def list_notes(self) -> list[IntakeNote]:
        result = []
        with self.jsonl_path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    attachments = []
                    for a in row.get("attachments", []):
                        path = self.files_dir / a["file"] if a.get("file") is not None else None
                        if path is not None and (Path(a["file"]).is_absolute() or
                                                 not path.resolve().is_relative_to(self.files_dir.resolve())):
                            raise ValueError("fixture file must be relative to files_dir")
                        attachments.append(Attachment(a["uuid"], a["kind"], a["type_uti"], path, a.get("problem")))
                    result.append(IntakeNote(row["note_id"], row["account_uuid"], row["title"],
                                             row["text"], datetime.fromisoformat(row["created"]),
                                             datetime.fromisoformat(row["modified"]), tuple(attachments)))
                except (KeyError, TypeError, ValueError) as exc:
                    raise NotesSourceError(f"Invalid fixture line {number}: {exc}") from exc
        return result
