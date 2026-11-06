"""Conservative Notes deletion and per-version media retirement."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Config
from .media import sha256_file
from .models import DONE, SUPERSEDED, DELETED, NotesControl
from .render import expire_images_in_record, parse_front_matter
from .report import ReportItem
from .state import StateDB


def vault_path(cfg: Config, relative: str) -> Path:
    path = (cfg.vault / relative).resolve()
    if not path.is_relative_to(cfg.vault.resolve()):
        raise ValueError("state path escapes vault")
    return path


def elapsed(now: datetime, then: datetime) -> timedelta:
    # Notes timestamps are local naive; run timestamps may carry a timezone.
    if now.tzinfo is not None and then.tzinfo is None:
        now = now.astimezone().replace(tzinfo=None)
    elif now.tzinfo is None and then.tzinfo is not None:
        then = then.astimezone().replace(tzinfo=None)
    return now - then


def run(cfg: Config, db: StateDB, control: NotesControl, now: datetime,
        dry_run: bool = False, changed_paths: set[str] | None = None) -> list[ReportItem]:
    items: list[ReportItem] = []
    changed_paths = changed_paths if changed_paths is not None else set()
    for row in db.rows_with_status(DONE):
        if not row.completed_at or elapsed(now, row.completed_at) < timedelta(days=cfg.delete_after_days):
            continue
        try:
            latest = db.latest(row.note_id)
            if latest is None or latest.fingerprint != row.fingerprint:
                raise ValueError("a newer version exists")
            if not row.record_path:
                raise ValueError("record path missing")
            metadata = parse_front_matter(vault_path(cfg, row.record_path).read_text())
            if metadata.get('note_id') != row.note_id or metadata.get('fingerprint') != row.fingerprint:
                raise ValueError("record identity does not match")
            if len(row.image_paths) != len(row.image_sha256):
                raise ValueError("image hashes missing")
            for path, digest in zip(row.image_paths, row.image_sha256):
                if sha256_file(vault_path(cfg, path)) != digest:
                    raise ValueError("image hash does not match")
            fresh = control.current_state(row.note_id)
            if fresh is None or fresh[0] != cfg.notes_folder or fresh[1] != row.note_modified:
                raise ValueError("fresh Notes folder or modification date does not match")
            if dry_run:
                items.append(ReportItem('cleanup', f'Would delete note: {row.title}'))
                continue
            control.delete(row.note_id)
            after = control.current_state(row.note_id)
            if after is not None and after[0] == cfg.notes_folder:
                raise ValueError("post-delete verification: note still in intake folder")
            row.status, row.deleted_at = DELETED, now
            db.upsert(row)
            items.append(ReportItem('cleanup', f'Deleted note: {row.title}'))
        except Exception as exc:
            items.append(ReportItem('cleanup', f'Deletion skipped for {row.title}: {exc}'))

    for row in db.rows_with_status(DONE, SUPERSEDED, DELETED):
        if not row.image_paths or not row.completed_at:
            continue
        if elapsed(now, row.completed_at) < timedelta(days=cfg.image_retention_days):
            continue
        try:
            if not row.record_path:
                raise ValueError('record path missing')
            record = vault_path(cfg, row.record_path)
            names = [Path(p).name for p in row.image_paths]
            text = record.read_text()
            rewritten = expire_images_in_record(text, names, now.date())
            # Identity protects against removing embeds from an unrelated file.
            if parse_front_matter(text).get('note_id') != row.note_id:
                raise ValueError('record identity does not match')
            if dry_run:
                items.append(ReportItem('cleanup', f'Would expire images: {row.title}'))
                continue
            # Include the rewrite even when a prior interrupted run already
            # removed the embeds but did not finish unlinking its files.
            changed_paths.add(row.record_path)
            previous_output = db.output(row.record_path)
            safe = (sha256_file(record) == row.record_sha256 if previous_output is None else
                    bool(previous_output['eligible']) and sha256_file(record) == previous_output['sha256'])
            db.remember_cleanup(row)
            db.remember_output(row.record_path, hashlib.sha256(rewritten.encode()).hexdigest(), eligible=safe)
            if text != rewritten:
                record.write_text(rewritten)
                # Other versions can share this record; keep their integrity hash current.
                for sibling in db.rows_with_status(DONE, SUPERSEDED, DELETED):
                    if sibling.record_path == row.record_path:
                        sibling.record_sha256 = sha256_file(record)
                        db.upsert(sibling)
            # Durable obligation survives cleared image paths, crashes and failed commits.
            db.remember_output(row.record_path, sha256_file(record), eligible=safe)
            # Persist the rewrite before unlink. Retain paths until all unlinks
            # succeed so an interrupted expiry removes its orphans next run.
            for path in row.image_paths:
                vault_path(cfg, path).unlink(missing_ok=True)
            row.image_paths, row.image_sha256 = [], []
            row.images_expired_at = now
            row.record_sha256 = sha256_file(record)
            db.upsert(row)

        except Exception as exc:
            items.append(ReportItem('cleanup', f'Image expiry skipped for {row.title}: {exc}'))
    if not dry_run:
        items.extend(ReportItem('cleanup', job['message']) for job in db.pending_cleanup())
    return items


def write_report_tracked(cfg: Config, db: StateDB, day: date, section: str) -> Path | None:
    """Append a section without blessing edits made since our previous write."""
    from . import report
    relative = (Path(cfg.inbox_dir) / f'Intake Run {day.isoformat()}.md').as_posix()
    path = vault_path(cfg, relative)
    previous = db.output(relative)
    safe = (not path.exists() if previous is None else
            bool(previous['eligible']) and path.is_file() and sha256_file(path) == previous['sha256'])
    if section.strip():
        existing = path.read_bytes() if path.exists() else b''
        prefix = f"# Intake Run {day.isoformat()}\n\n" if not existing else "\n"
        intended = existing + (prefix + section.rstrip() + "\n").encode('utf-8')
        # A crash after the append still leaves the expected bytes checkpointed.
        db.remember_output(relative, hashlib.sha256(intended).hexdigest(), eligible=safe)
    result = report.write_report(cfg.inbox_path, day, section)
    if result:
        db.remember_output(relative, sha256_file(result), eligible=safe)
        with db.connection:
            db.connection.execute("UPDATE cleanup_pending SET report_path=? WHERE identity IN "
                                  "(SELECT note_id || ':' || fingerprint FROM notes WHERE images_expired_at IS NOT NULL)",
                                  (relative,))
            if not cfg.git_commit:
                db.connection.execute("DELETE FROM cleanup_pending WHERE report_path IS NOT NULL")
    return result


def commit_candidates(cfg: Config, db: StateDB, items: list[ReportItem]) -> set[str]:
    paths = set()
    for output in db.pending_outputs():
        path = output['path']
        # Media is tracked for integrity, never for git.
        if Path(path).suffix.lower() not in {'.md'} and path != '.gitignore':
            continue
        current = vault_path(cfg, path)
        if output['eligible'] and current.is_file() and sha256_file(current) == output['sha256']:
            paths.add(path)
        elif path != '.gitignore':
            items.append(ReportItem('not_committed', f'{path} not committed: edited since intake wrote it'))
    return paths
