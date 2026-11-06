from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from chief_intake import cleanup
from chief_intake.config import Config
from chief_intake.media import sha256_file
from chief_intake.models import NoteRow
from chief_intake.notes_control import FakeNotesControl
from chief_intake.state import StateDB

NOW = datetime(2026, 11, 6)


@pytest.fixture
def case(tmp_path):
    cfg = Config('Intake', tmp_path / 'vault', 'Inbox', 'Inbox/board.md', (), 'Me',
                 state_dir=tmp_path / 'state')
    cfg.inbox_path.mkdir(parents=True)
    record = cfg.inbox_path / 'Intake Sample.md'
    record.write_text('---\nnote_id: synthetic\nfingerprint: abc\nimages: ["Intake Sample 1.jpg", "Intake Sample 2.jpg"]\n---\n![[Intake Sample 1.jpg]]\n![[Intake Sample 2.jpg]]\n')
    image = cfg.inbox_path / 'Intake Sample 1.jpg'
    image.write_bytes(b'synthetic photo')
    row = NoteRow('synthetic', 'abc', NOW - timedelta(days=20), status='done', title='Sample',
                  completed_at=NOW - timedelta(days=15), record_path='Inbox/Intake Sample.md',
                  record_sha256=sha256_file(record), image_paths=['Inbox/Intake Sample 1.jpg'],
                  image_sha256=[sha256_file(image)])
    control = FakeNotesControl({'synthetic': (cfg.notes_folder, row.note_modified)})
    with StateDB(cfg.state_dir / 'state.db') as db:
        db.upsert(row)
        yield cfg, db, row, control, record, image


@pytest.mark.parametrize('failure', ['record_missing', 'identity', 'fingerprint', 'image_missing',
                                     'image_hash', 'hash_missing', 'fresh_missing', 'folder',
                                     'modified', 'fresh_error', 'delete', 'verify', 'verify_error', 'newer'])
def test_each_deletion_check_fails_closed(case, monkeypatch, failure):
    cfg, db, row, control, record, image = case
    if failure == 'record_missing':
        record.unlink()
    elif failure in ('identity', 'fingerprint'):
        record.write_text(record.read_text().replace('synthetic' if failure == 'identity' else 'abc', 'wrong'))
    elif failure == 'image_missing':
        image.unlink()
    elif failure == 'image_hash':
        image.write_bytes(b'changed')
    elif failure == 'hash_missing':
        row.image_sha256 = []
        db.upsert(row)
    elif failure == 'fresh_missing':
        control.notes.clear()
    elif failure == 'folder':
        control.notes[row.note_id] = ('Other', row.note_modified)
    elif failure == 'modified':
        control.notes[row.note_id] = (cfg.notes_folder, NOW)
    elif failure == 'fresh_error':
        monkeypatch.setattr(control, 'current_state', lambda _: (_ for _ in ()).throw(RuntimeError('uncertain')))
    elif failure == 'delete':
        monkeypatch.setattr(control, 'delete', lambda _: (_ for _ in ()).throw(RuntimeError('delete failed')))
    elif failure == 'verify':
        monkeypatch.setattr(control, 'delete', lambda _: None)
    elif failure == 'verify_error':
        original = control.current_state
        def current(note_id):
            if control.deleted:
                raise RuntimeError('verification uncertain')
            return original(note_id)
        monkeypatch.setattr(control, 'current_state', current)
    else:
        db.upsert(replace(row, fingerprint='new', note_modified=NOW, status='pending'))
    items = cleanup.run(cfg, db, control, NOW)
    assert len(items) == 1 and 'Deletion skipped' in items[0].text
    assert db.get(row.note_id, row.fingerprint).status == 'done'


def test_happy_path_and_dry_run(case):
    cfg, db, row, control, _, _ = case
    assert 'Would delete' in cleanup.run(cfg, db, control, NOW, dry_run=True)[0].text
    assert not control.deleted
    assert 'Deleted note' in cleanup.run(cfg, db, control, NOW)[0].text
    assert control.deleted == [row.note_id]
    saved = db.get(row.note_id, row.fingerprint)
    assert saved.status == 'deleted' and saved.deleted_at == NOW
    assert cleanup.run(cfg, db, control, NOW) == []


@pytest.mark.parametrize('status', ['pending', 'waiting', 'in_progress', 'needs_attention', 'superseded'])
def test_non_done_never_deleted(case, status):
    cfg, db, row, control, _, _ = case
    row.status = status
    db.upsert(row)
    assert cleanup.run(cfg, db, control, NOW) == []
    assert not control.deleted


def test_expiry_per_version_and_orphans_after_crash(case, monkeypatch):
    cfg, db, row, control, record, image = case
    row.completed_at = NOW - timedelta(days=366)
    row.status = 'superseded'
    db.upsert(row)
    newer_image = cfg.inbox_path / 'Intake Sample 2.jpg'
    newer_image.write_bytes(b'new synthetic photo')
    newer = replace(row, fingerprint='new', status='done', note_modified=NOW,
                    completed_at=NOW, image_paths=['Inbox/Intake Sample 2.jpg'],
                    image_sha256=[sha256_file(newer_image)])
    db.upsert(newer)
    before = record.read_bytes()
    assert 'Would expire' in cleanup.run(cfg, db, control, NOW, True)[0].text
    assert record.read_bytes() == before
    unlink = type(image).unlink
    def crash(self, *args, **kwargs):
        if self == image:
            raise OSError('synthetic crash after rewrite')
        return unlink(self, *args, **kwargs)
    monkeypatch.setattr(type(image), 'unlink', crash)
    assert 'expiry skipped' in cleanup.run(cfg, db, control, NOW)[0].text
    assert 'Image expired' in record.read_text() and image.exists()
    assert '![[Intake Sample 2.jpg]]' in record.read_text()
    monkeypatch.setattr(type(image), 'unlink', unlink)
    paths = set()
    assert 'Images expired' in cleanup.run(cfg, db, control, NOW, changed_paths=paths)[0].text
    assert not image.exists() and newer_image.exists()
    assert db.get(row.note_id, row.fingerprint).images_expired_at == NOW
    assert db.get(row.note_id, newer.fingerprint).image_paths == newer.image_paths
    assert not control.deleted
