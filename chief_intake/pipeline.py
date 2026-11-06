"""Checkpointed orchestration. All live dependencies are supplied by the caller."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from . import board, cleanup, media, render, report
from .config import Config
from .dates import compute_flags
from .extract import extract_with_escalation, extraction_from_json, extraction_to_json
from .models import (DONE, IN_PROGRESS, NEEDS_ATTENTION, PENDING, WAITING, SUPERSEDED,
                     DELETED, ExtractionError, Extractor, IntakeNote, NoteRow,
                     NotesControl, NotesSource)
from .state import AlreadyRunning, StateDB, fingerprint, lock
from .vaultgit import GitVault, intake_ignore_patterns


def command_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


@dataclass
class RunResult:
    counts: dict[str, int] = field(default_factory=dict)
    items: list[report.ReportItem] = field(default_factory=list)
    report_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    already_running: bool = False
    out_dir: Path | None = None

    @property
    def success(self) -> bool:
        return not self.errors


def _versions(db: StateDB, note_id: str) -> list[NoteRow]:
    return [r for r in db.rows_with_status(PENDING, WAITING, IN_PROGRESS, DONE,
                                          NEEDS_ATTENTION, SUPERSEDED, DELETED)
            if r.note_id == note_id]


def _record(cfg: Config, db: StateDB, row: NoteRow, note: IntakeNote,
            now: datetime, runner=command_runner) -> None:
    extraction = extraction_from_json(row.extraction_json)
    prior = [r for r in _versions(db, row.note_id) if r.fingerprint != row.fingerprint and r.record_path]
    previous = prior[-1] if prior else None
    cfg.inbox_path.mkdir(parents=True, exist_ok=True)
    backlink = None
    if row.record_path is None:
        if previous:
            old = cleanup.vault_path(cfg, previous.record_path)
            if old.is_file() and media.sha256_file(old) == previous.record_sha256:
                row.record_path = previous.record_path
            else:
                root = Path(prior[0].record_path).stem
                n = len(prior) + 1
                candidate = cfg.inbox_path / f'{root} (update {n}).md'
                while candidate.exists():
                    n += 1
                    candidate = cfg.inbox_path / f'{root} (update {n}).md'
                row.record_path = candidate.relative_to(cfg.vault).as_posix()
        else:
            base = render.record_basename(note.created.date(), extraction.title,
                                          [p.name for p in cfg.inbox_path.glob('*.md')])
            row.record_path = (Path(cfg.inbox_dir) / (base + '.md')).as_posix()
        # Reserve image numbers and record identity before the first write.
        base = Path(row.record_path).stem
        n = 1
        for attachment in note.attachments:
            while any((cfg.inbox_path / render.image_name(base, n, ext)).exists()
                      for ext in (media.vault_suffix(attachment.path), ".jpg")):
                n += 1
            row.image_paths.append((Path(cfg.inbox_dir) / render.image_name(
                base, n, media.vault_suffix(attachment.path))).as_posix())
            if attachment.path.suffix.lower() in media.VIEWABLE_CONVERT:
                row.image_paths.append(str(Path(row.image_paths[-1]).with_suffix('.jpg')))
            n += 1
        db.upsert(row)
    if previous and row.record_path != previous.record_path:
        backlink = Path(previous.record_path).stem
    images: list[tuple[str, int | None]] = []
    # Keep old-version embeds when rewriting a shared record.
    for oldrow in prior:
        if oldrow.record_path == row.record_path:
            images.extend((Path(p).name, media.pdf_page_count(cleanup.vault_path(cfg, p))
                           if Path(p).suffix.lower() == '.pdf' and cleanup.vault_path(cfg, p).exists()
                           else None) for p in oldrow.image_paths
                          if Path(p).suffix.lower() not in media.VIEWABLE_CONVERT)
    row.image_sha256 = []
    targets = iter(row.image_paths)
    for attachment in note.attachments:
        original = cleanup.vault_path(cfg, next(targets))
        media.vault_copy(attachment.path, original, runner)
        written = [original]
        target = original
        if original.suffix.lower() in media.VIEWABLE_CONVERT:
            target = cleanup.vault_path(cfg, next(targets))
            media.vault_copy(attachment.path, target, runner)
            written.append(target)
        for path in written:
            digest = media.sha256_file(path)
            row.image_sha256.append(digest)
            db.remember_output(path.relative_to(cfg.vault).as_posix(), digest, eligible=False)
        images.append((target.name, media.pdf_page_count(target) if target.suffix.lower() == '.pdf' else None))
    text = render.render_record(note, extraction, compute_flags(extraction, now.date(), cfg.act_by_days),
                                images, now.date(), row.fingerprint, cfg.user_name,
                                updated=now.date() if previous else None)
    if backlink:
        text += f'\nUpdated from [[{backlink}]].\n'
    target = cleanup.vault_path(cfg, row.record_path)
    target.write_text(text, encoding='utf-8')
    row.record_sha256 = media.sha256_file(target)
    db.remember_output(row.record_path, row.record_sha256)
    row.owner = extraction.owner
    row.board_line = board.board_line(now.date(), cfg.board_from, extraction.owner,
                                      compute_flags(extraction, now.date(), cfg.act_by_days),
                                      extraction, target.stem, cfg.fallback_owner, cfg.user_name)
    row.stage_record_at = now
    db.upsert(row)


def _execute(cfg: Config, source: NotesSource, extractor: Extractor, control: NotesControl,
             git: GitVault, now: datetime, db: StateDB, work: Path, result: RunResult,
             dry_run: bool, limit: int | None) -> None:
    paths: set[str] = set()
    report_rows: list[NoteRow] = []
    board_safe = True
    git_ready = True
    if not dry_run and cfg.git_commit:
        try:
            patterns = intake_ignore_patterns(cfg.inbox_dir)
            ignore_clean = not git.dirty_vs_head('.gitignore')
            ignore_changed = git.ensure_ignored(Path('.gitignore'), patterns)
            if ignore_changed:
                db.remember_output('.gitignore', media.sha256_file(cfg.vault / '.gitignore'), eligible=False)
                if ignore_clean:
                    paths.add('.gitignore')
                else:
                    result.items.append(report.ReportItem('not_committed',
                                                          '.gitignore not committed: other uncommitted edits'))
            # Snapshot once, before any intake board write in this run.
            board_safe = not git.dirty_vs_head(cfg.board)
        except Exception as exc:
            git_ready, board_safe = False, False
            result.items.append(report.ReportItem('not_committed', f'git preflight: {exc}'))
            result.errors.append(str(exc))
    source_read = True
    try:
        notes = source.list_notes()
    except Exception as exc:
        source_read = False
        notes = []
        result.errors.append(f'Notes read failed: {exc}')
        result.items.append(report.ReportItem('couldnt', result.errors[-1]))
    by_id = {n.note_id: n for n in notes}
    resumed = db.incomplete()
    queue: list[tuple[NoteRow, IntakeNote | None]] = [(r, by_id.get(r.note_id)) for r in resumed]
    busy = {r.note_id for r in resumed}
    selected = 0
    for note in notes:
        if note.note_id in busy:
            continue
        if cleanup.elapsed(now, note.modified) < timedelta(minutes=cfg.min_note_age_minutes):
            continue
        if limit is not None and selected >= limit:
            break
        try:
            fp = fingerprint(note)
            row = db.get(note.note_id, fp)
            current = db.latest(note.note_id)
            if row and current and current.fingerprint == fp and row.status in (DONE, DELETED, NEEDS_ATTENTION):
                continue
            if row and (row.status == SUPERSEDED or (current and current.fingerprint != fp
                                                       and row.status in (DONE, DELETED, NEEDS_ATTENTION))):
                db.archive(row)
                row = None
            versions = _versions(db, note.note_id)
            completed = [r.completed_at for r in versions if r.completed_at]
            waiting = [r.first_seen_at for r in versions if r.status == WAITING and r.first_seen_at
                       and (not completed or r.first_seen_at > max(completed))]
            row = row or NoteRow(note.note_id, fp, note.modified, title=note.title,
                                 first_seen_at=min(waiting) if waiting else now)
            db.upsert(row)
            queue.append((row, note))
            selected += 1
        except Exception as exc:
            result.items.append(report.ReportItem('couldnt', f'{note.title}: {exc}'))
            result.errors.append(str(exc))
    if not board_safe and (queue or db.uncommitted_done()) and git_ready:
        result.items.append(report.ReportItem('not_committed', 'board not committed: other uncommitted edits'))
    for row, note in queue:
        try:
            if not row.extraction_json:
                if note is None:
                    raise ValueError('note unavailable before record stage')
                if any(a.kind == 'other' for a in note.attachments):
                    row.status, row.last_error = NEEDS_ATTENTION, "contains an attachment intake can't read"
                elif any(a.problem or a.path is None or not a.path.is_file() for a in note.attachments):
                    row.status = WAITING
                    row.last_error = 'media not synced or not ready'
                else:
                    try:
                        parts = media.prepare(note.attachments, work / uuid.uuid4().hex,
                                              getattr(git, 'runner', command_runner))
                    except media.MediaError as exc:
                        row.status, row.last_error = NEEDS_ATTENTION, str(exc)
                    else:
                        try:
                            extraction = extract_with_escalation(extractor, note, parts, now.date(), cfg)
                        except ExtractionError as exc:
                            row.attempts += 1
                            row.status = NEEDS_ATTENTION if row.attempts >= 3 else PENDING
                            row.last_error = f'failed {row.attempts} times: {exc}'
                        else:
                            row.extraction_json = extraction_to_json(extraction)
                            if extraction.readable:
                                row.status = IN_PROGRESS
                            else:
                                row.status = NEEDS_ATTENTION
                                row.last_error = "couldn't read: " + (extraction.unreadable_reason or 'unreadable')
                db.upsert(row)
                if row.status != IN_PROGRESS:
                    if row.status != WAITING or cleanup.elapsed(now, row.first_seen_at) > timedelta(hours=24):
                        result.items.append(report.ReportItem('waiting' if row.status == WAITING else 'couldnt',
                                                              f'{row.title}: {row.last_error}'))
                    continue
            if row.status != IN_PROGRESS:
                continue
            if not row.stage_record_at:
                if note is None:
                    raise ValueError('note unavailable before record stage')
                if fingerprint(note) != row.fingerprint:
                    row.status = SUPERSEDED
                    raise ValueError('note changed before record stage; current version will be retried next run')
                _record(cfg, db, row, note, now, getattr(git, 'runner', command_runner))
            paths.add(row.record_path)
            previous = [r for r in _versions(db, row.note_id) if r.fingerprint != row.fingerprint and r.record_path]
            if not row.stage_board_at:
                if not dry_run:
                    oldbase = Path(previous[-1].record_path).stem if previous else None
                    board_result = board.ensure_line(cfg.board_path, cfg.board_section, row.board_line,
                                      Path(row.record_path).stem, bool(previous),
                                      previous_record_basename=oldbase,
                                      previous_line=previous[-1].board_line if previous else None)
                    if board_result == 'updated-appended':
                        row.board_line = board._updated(row.board_line)
                    db.remember_output(cfg.board, media.sha256_file(cfg.board_path), eligible=board_safe)
                    if board_safe:
                        paths.add(cfg.board)
                row.stage_board_at = now
                db.upsert(row)
            if not row.stage_report_at:
                e = extraction_from_json(row.extraction_json)
                flags = compute_flags(e, now.date(), cfg.act_by_days)
                link = Path(row.record_path).stem
                result.items.append(report.ReportItem('updated' if previous else 'routed',
                                                      f'{e.title} → {e.owner}' +
                                                      ('; edited record preserved in update file' if '(update ' in link else ''), link))
                if flags.overdue or flags.due_soon:
                    result.items.append(report.ReportItem('overdue' if flags.overdue else 'due_soon',
                                                          flags.label or e.title, link))
                report_rows.append(row)
        except Exception as exc:
            row.last_error = str(exc)
            db.upsert(row)
            result.errors.append(f'{row.title}: {exc}')
            result.items.append(report.ReportItem('waiting' if row.stage_record_at else 'couldnt',
                                                  f'{row.title}: {exc}'))
    # Finish interrupted rows whose report checkpoint was saved before status.
    for row, _ in queue:
        if row.stage_record_at and row.stage_board_at and row.stage_report_at and row.status == IN_PROGRESS:
            row.status, row.completed_at = DONE, row.completed_at or now
            row.last_error = None
            db.upsert(row)
            db.supersede_older(row.note_id, row.fingerprint)
    if not dry_run and source_read:
        result.items.extend(cleanup.run(cfg, db, control, now, changed_paths=paths))
    try:
        result.report_path = cleanup.write_report_tracked(cfg, db, now.date(),
                                                 report.render_run_section(now, result.items))
        if result.report_path:
            paths.add(result.report_path.relative_to(cfg.vault).as_posix())
        for row in report_rows:
            row.stage_report_at = now
            db.upsert(row)
            row.status, row.completed_at = DONE, now
            row.last_error = None
            db.upsert(row)
            db.supersede_older(row.note_id, row.fingerprint)
    except Exception as exc:
        result.errors.append(f'Report write failed: {exc}')
    if dry_run or not cfg.git_commit:
        return
    retry = db.uncommitted_done()
    ignore_allowed = '.gitignore' in paths
    before_holds = len(result.items)
    paths = cleanup.commit_candidates(cfg, db, result.items)
    if len(result.items) > before_holds:
        result.report_path = cleanup.write_report_tracked(
            cfg, db, now.date(), report.render_run_section(now, result.items[before_holds:]))
        paths = cleanup.commit_candidates(cfg, db, [])
    if ignore_allowed:
        output = db.output('.gitignore')
        if not output or media.sha256_file(cfg.vault / '.gitignore') != output['sha256']:
            ignore_allowed = False
            result.items.append(report.ReportItem('not_committed', '.gitignore not committed: edited since intake wrote it'))
    if not ignore_allowed:
        paths.discard('.gitignore')
    elif ignore_clean:
        paths.add('.gitignore')
    if not board_safe:
        paths.discard(cfg.board)
    if not paths:
        return
    try:
        if not git_ready:
            raise ValueError('git preflight failed')
        images = [p for row in retry for p in row.image_paths]
        if not git.check_ignored(images):
            raise ValueError('intake images are not ignored; commit refused')
        outcome = git.commit(sorted(paths), f'Chief Intake: {len(report_rows)} processed (run {now:%Y-%m-%d %H:%M})')
        if outcome not in ('committed', 'nothing'):
            raise ValueError(outcome)
        db.committed_outputs(sorted(paths))
        for row in retry:
            record_output = db.output(row.record_path) if row.record_path else None
            report_relative = (Path(cfg.inbox_dir) / f'Intake Run {row.stage_report_at:%Y-%m-%d}.md').as_posix() if row.stage_report_at else None
            report_output = db.output(report_relative) if report_relative else None
            if (not record_output or record_output['pending'] or
                    not report_output or report_output['pending']):
                continue
            row.stage_commit_at = now
            db.upsert(row)
    except Exception as exc:
        result.errors.append(f'Commit failed: {exc}')
        item = report.ReportItem('not_committed', f'Commit failed: {exc}')
        result.items.append(item)
        try:
            result.report_path = cleanup.write_report_tracked(cfg, db, now.date(), report.render_run_section(now, [item]))
        except Exception:
            pass


def run(cfg: Config, source: NotesSource, extractor: Extractor, control: NotesControl,
        git: GitVault, now: datetime, dry_run: bool = False, out_dir: Path | None = None,
        limit: int | None = None, trigger: str = "terminal") -> RunResult:
    if limit is not None and limit < 0:
        raise ValueError('limit must be nonnegative')
    result = RunResult()
    if dry_run:
        output = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix='chief-intake-'))
        output.mkdir(parents=True, exist_ok=True)
        result.out_dir = output
        cfg = replace(cfg, vault=output, inbox_dir='.', state_dir=output)
    work = cfg.state_dir / 'work' / uuid.uuid4().hex
    original_work = getattr(extractor, 'work_dir', None)
    if original_work is not None:
        extractor.work_dir = work
    def execute() -> None:
        with StateDB(Path(':memory:') if dry_run else cfg.state_dir / 'state.db') as db:
            run_id = None if dry_run else db.start_run()
            try:
                _execute(cfg, source, extractor, control, git, now, db, work, result, dry_run, limit)
            finally:
                result.counts = dict(Counter(item.category for item in result.items))
                result.counts['processed'] = len({i.link for i in result.items if i.category in ('routed', 'updated')})
                if run_id is not None:
                    db.finish_run(run_id, result.counts, False)
    try:
        if dry_run:
            execute()
        else:
            with lock(cfg.state_dir):
                execute()
    except AlreadyRunning:
        result.already_running = True
    except Exception as exc:
        result.errors.append(str(exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if work.parent.exists() and not any(work.parent.iterdir()):
            work.parent.rmdir()
        if original_work is not None:
            extractor.work_dir = original_work
        if not dry_run and not result.already_running:
            report.write_last_run(cfg.state_dir, {'started': now.isoformat(), 'trigger': trigger, 'success': result.success,
                                  'counts': result.counts, 'items': [asdict(i) for i in result.items],
                                  'errors': result.errors, 'report_path': str(result.report_path) if result.report_path else None})
    return result
