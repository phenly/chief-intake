from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from chief_intake.models import Attachment, IntakeNote, NoteRow
from chief_intake.state import AlreadyRunning, StateDB, fingerprint, lock


def test_crud_stages_queries_and_runs(tmp_path):
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    row = NoteRow('synthetic-1', 'abc', now, status='in_progress',
                  image_paths=['Intake Sample 1.jpg'], image_sha256=['abc'], first_seen_at=now)
    path = tmp_path / 'state.db'
    with StateDB(path) as db:
        assert db.get('absent', 'absent') is None
        db.upsert(row)
        assert db.get(row.note_id, row.fingerprint) == row
        assert db.incomplete() == [row]
        # Checkpoint every stage and re-open to model a crash at each boundary.
        for field in ('stage_record_at', 'stage_board_at', 'stage_report_at'):
            setattr(row, field, now)
            db.upsert(row)
            with StateDB(path) as reopened:
                assert reopened.incomplete() == [row]
        row.status = 'done'
        row.completed_at = now
        db.upsert(row)
        assert db.incomplete() == []
        assert db.uncommitted_done() == [row]
        row.stage_commit_at = now
        db.upsert(row)
        assert db.uncommitted_done() == []
        newer = replace(row, fingerprint='def', note_modified=now + timedelta(days=1), status='pending')
        db.upsert(newer)
        assert db.latest(row.note_id) == newer
        db.supersede_older(row.note_id, newer.fingerprint)
        assert db.get(row.note_id, row.fingerprint).status == 'superseded'
        assert db.rows_with_status('pending') == [newer]
        assert db.rows_with_status() == []
        run = db.start_run()
        db.finish_run(run, {'processed': 1}, False)
        raw = db.connection.execute('SELECT * FROM runs WHERE id=?', (run,)).fetchone()
        assert raw['finished'] and raw['counts'] == '{"processed": 1}' and raw['dry_run'] == 0
        with pytest.raises(ValueError, match='unknown run'):
            db.finish_run(9999, {}, True)


def test_lock_contention_and_release(tmp_path):
    with lock(tmp_path):
        with pytest.raises(AlreadyRunning):
            with lock(tmp_path):
                pytest.fail('second lock acquired')
    with pytest.raises(RuntimeError):
        with lock(tmp_path):
            raise RuntimeError('synthetic failure')
    with lock(tmp_path):
        pass


def test_fingerprint(tmp_path):
    now = datetime(2026, 11, 6)
    media = tmp_path / 'sample.jpg'
    media.write_bytes(b'synthetic image')
    note = IntakeNote('synthetic-1', 'account', 'Title', 'Synthetic text', now, now,
                      (Attachment('a1', 'photo', 'public.jpeg', media),))
    first = fingerprint(note)
    assert len(first) == 64 and fingerprint(note) == first
    assert fingerprint(replace(note, text='Changed text')) != first
    assert fingerprint(replace(note, modified=now + timedelta(days=1))) == first
    assert fingerprint(replace(note, attachments=(replace(note.attachments[0], uuid='a2'),))) != first
    media.write_bytes(b'changed synthetic image')
    assert fingerprint(note) != first
    media.unlink()
    missing = fingerprint(note)
    assert missing != first
    assert fingerprint(replace(note, attachments=(replace(note.attachments[0], path=None),))) == missing


def test_text_only_fingerprint_and_durable_upsert(tmp_path):
    import hashlib
    now = datetime(2026, 11, 6)
    note = IntakeNote('synthetic-1', 'account', 'Title', 'Synthetic text', now, now)
    assert fingerprint(note) == hashlib.sha256(b'Synthetic text').hexdigest()
    path = tmp_path / 'state.db'
    row = NoteRow('synthetic-1', 'abc', now)
    with StateDB(path) as db:
        db.upsert(row)
        with StateDB(path) as reader:
            assert reader.get(row.note_id, row.fingerprint) == row
