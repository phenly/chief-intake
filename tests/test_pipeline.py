"""End-to-end integration using only synthetic Notes and temporary git vaults."""
import json
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from chief_intake import pipeline
from chief_intake.config import load
from chief_intake.extract import FakeExtractor
from chief_intake.models import Extraction, ExtractionError
from chief_intake.notes_control import FakeNotesControl
from chief_intake.notes_source import FixtureSource
from chief_intake.state import StateDB, fingerprint, lock
from chief_intake.vaultgit import GitVault

FIXTURES = Path(__file__).parent / 'fixtures'
NOW = datetime(2026, 11, 6, 12)


def runner(argv):
    if argv[0] == 'sips':
        if '--out' in argv:
            shutil.copyfile(argv[argv.index('--out') - 1], argv[-1])
        return subprocess.CompletedProcess(argv, 0, 'pixelWidth: 100\npixelHeight: 100\n', '')
    return subprocess.run(argv, capture_output=True, text=True)


class Source:
    def __init__(self, notes):
        self.notes = notes

    def list_notes(self):
        return self.notes


@pytest.fixture
def env(tmp_path):
    vault = tmp_path / 'vault'
    (vault / 'Inbox').mkdir(parents=True)
    cfg = replace(load(FIXTURES / 'config.toml'), vault=vault, state_dir=tmp_path / 'state')
    cfg.board_path.write_text('# Board\n\n## Open')
    def git(*args):
        result = runner(['git', '-C', str(vault), *args])
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()
    git('init')
    git('config', 'user.name', 'J. Sample')
    git('config', 'user.email', 'sample@example.com')
    git('add', 'Inbox/board.md')
    git('commit', '-m', 'Synthetic initial board')
    notes = FixtureSource(FIXTURES / 'notes.jsonl', FIXTURES).list_notes()
    answers = json.loads((FIXTURES / 'answers.json').read_text())
    keys = [json.loads(line)['key'] for line in (FIXTURES / 'notes.jsonl').read_text().splitlines()]
    canned = {}
    for note, key in zip(notes, keys):
        answer = answers[key]
        canned[note.note_id] = Extraction(answer['readable'], 'notice', note.title,
            'Synthetic summary', answer.get('owner', 'Me'), 'Check household records.',
            sender=answer.get('sender', 'Sample sender'), amount_due=answer.get('amount_due'),
            unreadable_reason='blurry' if not answer['readable'] else None, model='haiku')
    source = Source(notes)
    extractor = FakeExtractor(canned)
    control = FakeNotesControl({n.note_id: (cfg.notes_folder, n.modified) for n in notes})
    return cfg, source, extractor, control, GitVault(vault, runner), git


def invoke(env, **kwargs):
    return pipeline.run(*env[:5], NOW, **kwargs)


def rows(env):
    with StateDB(env[0].state_dir / 'state.db') as db:
        return db.rows_with_status('done', 'in_progress', 'pending', 'needs_attention', 'superseded', 'waiting')


def test_full_run_idempotence_update_and_only_commit(env):
    cfg, source, extractor, control, _, git = env
    (cfg.vault / 'unrelated.md').write_text('Other writer')
    git('add', 'unrelated.md')
    first = invoke(env)
    assert first.success, first.errors
    assert first.counts['processed'] == 11
    assert first.counts['couldnt'] == 1
    assert len(list(cfg.inbox_path.glob('*.jpg'))) == 7
    assert len(list(cfg.inbox_path.glob('*.pdf'))) == 1
    assert cfg.board_path.read_text().count(' · [[') == 11
    names = git('show', '--pretty=format:', '--name-only', 'HEAD').splitlines()
    assert '.gitignore' in names and cfg.board in names
    assert all(p == '.gitignore' or p.startswith('Inbox/') for p in names)
    assert all(Path(p).suffix == '.md' or p == '.gitignore' for p in names)
    assert git('diff', '--cached', '--name-only') == 'unrelated.md'
    report = first.report_path.read_bytes()
    head = git('rev-parse', 'HEAD')
    calls = len(extractor.calls)
    second = invoke(env)
    assert second.success and second.report_path is None
    assert git('rev-parse', 'HEAD') == head
    assert first.report_path.read_bytes() == report and len(extractor.calls) == calls
    assert control.deleted == []
    source.notes[0] = replace(source.notes[0], text='Changed synthetic note', modified=NOW - timedelta(hours=1))
    extractor.answers[source.notes[0].note_id] = replace(extractor.answers[source.notes[0].note_id], owner='Home', amount_due='$20')
    third = invoke(env)
    assert third.success and third.counts['updated'] == 1
    assert cfg.board_path.read_text().count(' · [[') == 11
    assert '→ Home' in cfg.board_path.read_text()
    assert len([r for r in rows(env) if r.status == 'superseded']) == 1
    assert not (cfg.state_dir / 'work').exists()
    for row in rows(env):
        if row.status == 'done':
            assert row.record_sha256 and len(row.image_paths) == len(row.image_sha256)
            assert row.stage_commit_at and row.completed_at


def test_edited_record_preserved_and_board_relinked(env):
    env[1].notes = env[1].notes[:1]
    assert invoke(env).success
    row = rows(env)[0]
    record = env[0].vault / row.record_path
    record.write_text(record.read_text() + '\nHuman annotation.\n')
    original = record.read_bytes()
    env[1].notes[0] = replace(env[1].notes[0], text='Changed note', modified=NOW - timedelta(hours=1))
    assert invoke(env).counts['updated'] == 1
    assert record.read_bytes() == original
    updated = next(env[0].inbox_path.glob('* (update 2).md'))
    assert f'Updated from [[{record.stem}]]' in updated.read_text()
    assert f'[[{updated.stem}]]' in env[0].board_path.read_text()
    assert env[0].board_path.read_text().count(' · [[') == 1


def test_failures_retry_fingerprint_and_unreadable_never_deleted(env):
    cfg, source, extractor, control, _, _ = env
    source.notes = [source.notes[7], source.notes[8]]
    bad = source.notes[1]
    extractor.answers[bad.note_id] = ExtractionError('synthetic extraction error')
    for attempt in range(1, 4):
        invoke(env)
        assert next(r for r in rows(env) if r.note_id == bad.note_id).attempts == attempt
    invoke(env)
    assert len(extractor.calls) == 5  # unreadable calls both models once, three failed attempts
    assert all(r.status == 'needs_attention' for r in rows(env))
    result = pipeline.run(*env[:5], NOW + timedelta(days=30))
    assert not control.deleted and ' · [[' not in cfg.board_path.read_text()
    assert result.counts['processed'] == 0
    source.notes[1] = replace(bad, text='New content')
    invoke(env)
    assert next(r for r in rows(env) if r.fingerprint == fingerprint(source.notes[1])).attempts == 1


@pytest.mark.parametrize('stage', ['record', 'board', 'report', 'commit'])
def test_crash_at_stage_boundary_resumes(env, monkeypatch, stage):
    env[1].notes = env[1].notes[:1]
    class Crash(BaseException):
        pass
    target, name = {'record': (pipeline, '_record'), 'board': (pipeline.board, 'ensure_line'),
                    'report': (pipeline.report, 'write_report'), 'commit': (env[4], 'commit')}[stage]
    original = getattr(target, name)
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise Crash()
    monkeypatch.setattr(target, name, crash)
    with pytest.raises(Crash):
        invoke(env)
    monkeypatch.setattr(target, name, original)
    before = len(env[2].calls)
    result = invoke(env)
    assert result.success, result.errors
    assert len(env[2].calls) == before
    assert env[0].board_path.read_text().count(' · [[') == 1
    assert len(list(env[0].inbox_path.glob('Intake 2026*.md'))) == 1
    assert rows(env)[0].status == 'done' and rows(env)[0].stage_commit_at


def test_dirty_board_excluded_and_failed_commit_retried(env, monkeypatch):
    cfg, source, _, _, gitvault, git = env
    source.notes = source.notes[:1]
    cfg.board_path.write_text(cfg.board_path.read_text() + '\nOther writer\n')
    first = invoke(env)
    assert first.success
    assert 'not committed' in first.report_path.read_text()
    assert cfg.board not in git('show', '--pretty=format:', '--name-only', 'HEAD').splitlines()
    assert 'Other writer' in cfg.board_path.read_text()
    source.notes[0] = replace(source.notes[0], text='changed')
    commit = gitvault.commit
    monkeypatch.setattr(gitvault, 'commit', lambda *a: 'synthetic index lock')
    assert not invoke(env).success
    assert any(r.status == 'done' and r.stage_commit_at is None for r in rows(env))
    monkeypatch.setattr(gitvault, 'commit', commit)
    assert invoke(env).success
    assert all(r.stage_commit_at for r in rows(env) if r.status == 'done')


def test_waiting_age_unsupported_board_hold_and_lock(env):
    cfg, source, _, _, _, _ = env
    note = source.notes[0]
    source.notes = [replace(note, modified=NOW)]
    assert invoke(env).counts['processed'] == 0
    source.notes = [replace(note, attachments=(replace(note.attachments[0], path=None),))]
    assert invoke(env).report_path is None
    later = pipeline.run(*env[:5], NOW + timedelta(hours=25))
    assert later.counts['waiting'] == 1
    source.notes = [replace(note, attachments=(replace(note.attachments[0], kind='other'),), text='unsupported')]
    assert invoke(env).counts['couldnt'] == 1
    source.notes = [note]
    cfg.board_path.write_text('## Open\n## Closed\n')
    assert invoke(env).counts['waiting'] == 1
    assert any(r.status == 'in_progress' for r in rows(env))
    cfg.board_path.write_text('## Open\n')
    assert invoke(env).counts['processed'] == 1
    with lock(cfg.state_dir):
        assert invoke(env).already_running


def test_dry_run_writes_only_output_and_read_failure_journal(env, tmp_path):
    cfg = env[0]
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    output = tmp_path / 'output'
    result = invoke(env, dry_run=True, out_dir=output)
    assert result.success and result.report_path.is_relative_to(output)
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file() and not p.is_relative_to(output)}
    assert before == after and not cfg.state_dir.exists()
    class Broken:
        def list_notes(self):
            raise PermissionError('synthetic permission denial')
    result = pipeline.run(cfg, Broken(), *env[2:5], NOW)
    assert not result.success
    assert json.loads((cfg.state_dir / 'last-run.json').read_text())['success'] is False
    assert (cfg.state_dir / 'runs.log').exists()


def test_vault_write_failure_is_journaled(env, monkeypatch):
    cfg = env[0]
    write = Path.write_text
    def blocked(self, *args, **kwargs):
        if self.is_relative_to(cfg.vault):
            raise PermissionError('synthetic vault permission denial')
        return write(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', blocked)
    result = invoke(env)
    assert not result.success
    outcome = json.loads((cfg.state_dir / 'last-run.json').read_text())
    assert outcome['errors'] and not outcome['success']
    assert (cfg.state_dir / 'runs.log').read_text()
    assert not env[3].deleted


def test_empty_run_with_dirty_board_does_not_write_report(env):
    cfg, source, _, _, _, git = env
    assert invoke(env).success
    source.notes = []
    head = git('rev-parse', 'HEAD')
    cfg.board_path.write_text(cfg.board_path.read_text() + '\nOther writer\n')
    result = invoke(env)
    assert result.success and result.report_path is None
    assert git('rev-parse', 'HEAD') == head


def test_changed_note_after_crash_before_record_can_progress(env, monkeypatch):
    env[1].notes = env[1].notes[:1]
    class Crash(BaseException):
        pass
    original = pipeline._record
    def crash(*args):
        raise Crash()
    monkeypatch.setattr(pipeline, '_record', crash)
    with pytest.raises(Crash):
        invoke(env)
    monkeypatch.setattr(pipeline, '_record', original)
    env[1].notes[0] = replace(env[1].notes[0], text='Edited after crash', modified=NOW - timedelta(hours=1))
    assert not invoke(env).success
    assert invoke(env).counts['processed'] == 1
    assert env[0].board_path.read_text().count(' · [[') == 1


def test_fresh_deletion_after_retention_and_original_image_integrity(env):
    cfg, source, _, control, _, _ = env
    source.notes = source.notes[:1]
    assert invoke(env).success
    row = rows(env)[0]
    assert (cfg.vault / row.image_paths[0]).read_bytes() == source.notes[0].attachments[0].path.read_bytes()
    assert not pipeline.run(*env[:5], NOW + timedelta(days=13)).counts.get('cleanup')
    result = pipeline.run(*env[:5], NOW + timedelta(days=14))
    assert result.success and result.counts['cleanup'] == 1
    assert control.deleted == [row.note_id]
    with StateDB(cfg.state_dir / 'state.db') as db:
        assert db.get(row.note_id, row.fingerprint).status == 'deleted'


@pytest.mark.parametrize('extension', ['heic', 'heif'])
def test_heic_original_and_viewing_copy_are_tracked_and_verified(env, tmp_path, monkeypatch, extension):
    from chief_intake.media import sha256_file
    cfg, source, _, control, gitvault, _ = env
    original = tmp_path / ('synthetic.' + extension)
    original.write_bytes(b'synthetic original HEIC bytes')
    note = source.notes[0]
    source.notes = [replace(note, attachments=(replace(note.attachments[0], path=original),))]
    def convert(argv):
        if argv[0] == 'sips' and '--out' in argv:
            Path(argv[-1]).write_bytes(b'synthetic viewing JPEG')
            return subprocess.CompletedProcess(argv, 0, '', '')
        return runner(argv)
    gitvault.runner = convert
    monkeypatch.setattr(pipeline, "command_runner", convert)
    assert invoke(env).success
    row = rows(env)[0]
    assert [Path(p).suffix for p in row.image_paths] == ['.' + extension, '.jpg']
    assert (cfg.vault / row.image_paths[0]).read_bytes() == original.read_bytes()
    assert row.image_sha256 == [sha256_file(cfg.vault / p) for p in row.image_paths]
    text = (cfg.vault / row.record_path).read_text()
    assert '![[%s]]' % Path(row.image_paths[1]).name in text
    assert '![[%s]]' % Path(row.image_paths[0]).name not in text
    assert gitvault.check_ignored(row.image_paths)
    # Corrupt only the original: the JPEG cannot authorize source deletion.
    (cfg.vault / row.image_paths[0]).write_bytes(b'changed')
    result = pipeline.run(*env[:5], NOW + timedelta(days=14))
    assert not control.deleted
    assert 'image hash does not match' in result.report_path.read_text()
    assert pipeline.run(*env[:5], NOW + timedelta(days=366)).success
    assert all(not (cfg.vault / p).exists() for p in row.image_paths)


def test_board_update_preserves_human_annotation(env):
    cfg, source, _, _, _, _ = env
    source.notes = source.notes[:1]
    assert invoke(env).success
    old_line = rows(env)[0].board_line
    annotated = old_line + ' · Sam is handling this'
    cfg.board_path.write_text(cfg.board_path.read_text().replace(old_line, annotated))
    source.notes[0] = replace(source.notes[0], text='Updated synthetic content')
    assert invoke(env).success
    assert annotated in cfg.board_path.read_text().splitlines()
    assert cfg.board_path.read_text().count('UPDATED:') == 1
    # The actual UPDATED line is the next replacement's exact identity.
    source.notes[0] = replace(source.notes[0], text='Another synthetic update')
    assert invoke(env).success
    assert annotated in cfg.board_path.read_text().splitlines()
    assert cfg.board_path.read_text().count('UPDATED:') <= 1


def test_retry_commit_excludes_edited_outputs_and_unrelated_gitignore(env, monkeypatch):
    cfg, source, _, _, gitvault, git = env
    source.notes = source.notes[:1]
    commit = gitvault.commit
    monkeypatch.setattr(gitvault, 'commit', lambda *a: 'synthetic commit failure')
    first = invoke(env)
    assert not first.success
    row = rows(env)[0]
    record = cfg.vault / row.record_path
    record.write_text(record.read_text() + '\nSam annotation\n')
    first.report_path.write_text(first.report_path.read_text() + '\nAlex annotation\n')
    (cfg.vault / '.gitignore').write_text((cfg.vault / '.gitignore').read_text() + 'personal-pattern\n')
    monkeypatch.setattr(gitvault, 'commit', commit)
    held = invoke(env)
    assert not rows(env)[0].stage_commit_at
    assert 'not committed: edited since intake wrote it' in held.report_path.read_text()
    assert git('show', '--pretty=format:', '--name-only', 'HEAD') == cfg.board
    assert 'Sam annotation' in record.read_text()
    assert '.gitignore' in git('status', '--porcelain')


def test_returning_fingerprint_is_reprocessed_with_fresh_clocks(env):
    cfg, source, extractor, _, _, _ = env
    original = source.notes[0]
    source.notes = [original]
    assert invoke(env).success
    source.notes = [replace(original, text='Version B', modified=NOW - timedelta(minutes=30))]
    assert pipeline.run(*env[:5], NOW + timedelta(days=1)).success
    source.notes = [replace(original, modified=NOW + timedelta(days=2, hours=-1))]
    result = pipeline.run(*env[:5], NOW + timedelta(days=2))
    assert result.counts['updated'] == 1
    assert len(extractor.calls) == 3
    current = next(r for r in rows(env) if r.status == 'done')
    assert current.fingerprint == fingerprint(original)
    assert current.completed_at == NOW + timedelta(days=2)
    assert current.note_modified == source.notes[0].modified
    assert (cfg.vault / current.record_path).read_text().count('![[%s' % Path(current.record_path).stem) == 3


def test_waiting_clock_survives_incremental_sync_and_resets_after_success(env):
    cfg, source, _, _, _, _ = env
    note = source.notes[0]
    attachment = note.attachments[0]
    source.notes = [replace(note, attachments=(replace(attachment, path=None),))]
    assert invoke(env).report_path is None
    source.notes = [replace(note, attachments=(attachment, replace(attachment, uuid='second', path=None)))]
    assert pipeline.run(*env[:5], NOW + timedelta(hours=20)).report_path is None
    source.notes = [replace(source.notes[0], text='More sync progress')]
    result = pipeline.run(*env[:5], NOW + timedelta(hours=25))
    assert result.counts['waiting'] == 1
    source.notes = [note]
    assert pipeline.run(*env[:5], NOW + timedelta(hours=26)).counts['processed'] == 1
    source.notes = [replace(note, text='New waiting version', attachments=(replace(attachment, path=None),))]
    assert pipeline.run(*env[:5], NOW + timedelta(hours=27)).report_path is None


def test_expiry_commit_obligation_survives_failed_commit(env, monkeypatch):
    cfg, source, _, _, gitvault, git = env
    source.notes = source.notes[:1]
    assert invoke(env).success
    row = rows(env)[0]
    commit = gitvault.commit
    monkeypatch.setattr(gitvault, 'commit', lambda *a: 'synthetic expiry commit failure')
    expiry = pipeline.run(*env[:5], NOW + timedelta(days=366))
    assert not expiry.success
    assert all(not (cfg.vault / p).exists() for p in row.image_paths)
    monkeypatch.setattr(gitvault, 'commit', commit)
    retry = pipeline.run(*env[:5], NOW + timedelta(days=367))
    assert retry.success
    names = git('show', '--pretty=format:', '--name-only', 'HEAD').splitlines()
    assert row.record_path in names
    assert expiry.report_path.relative_to(cfg.vault).as_posix() in names
    assert 'Images expired:' in retry.report_path.read_text()
    assert pipeline.run(*env[:5], NOW + timedelta(days=368)).report_path is None


def test_dirty_gitignore_before_first_run_is_never_committed(env):
    cfg, source, _, _, _, git = env
    source.notes = source.notes[:1]
    ignore = cfg.vault / '.gitignore'
    ignore.write_text('personal-pattern\n')
    assert invoke(env).success
    assert '.gitignore' not in git('show', '--pretty=format:', '--name-only', 'HEAD').splitlines()
    assert ignore.read_text().startswith('personal-pattern\n')


def test_expiry_obligation_survives_crash_before_report(env, monkeypatch):
    cfg, source, _, _, _, git = env
    source.notes = source.notes[:1]
    assert invoke(env).success
    row = rows(env)[0]
    original = pipeline.cleanup.run
    class Crash(BaseException):
        pass
    def interrupted(*a, **kw):
        original(*a, **kw)
        raise Crash()
    monkeypatch.setattr(pipeline.cleanup, 'run', interrupted)
    with pytest.raises(Crash):
        pipeline.run(*env[:5], NOW + timedelta(days=366))
    monkeypatch.setattr(pipeline.cleanup, 'run', original)
    result = pipeline.run(*env[:5], NOW + timedelta(days=367))
    assert result.success
    assert row.record_path in git('show', '--pretty=format:', '--name-only', 'HEAD').splitlines()
    assert 'Images expired:' in result.report_path.read_text()
