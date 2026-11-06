from dataclasses import replace
from datetime import date

import pytest

from chief_intake.board import BoardError, board_line, ensure_line
from chief_intake.models import Extraction, Flags


def wanted(**changes):
    e = replace(Extraction(True, 'bill', 'Water bill', 'Summary', 'Sam', 'Pay online.',
                           amount_due='$12', due_date=date(2026, 11, 15)), **changes)
    return board_line(date(2026, 11, 6), 'Intake', 'Sam',
                      Flags(False, True, date(2026, 11, 8), ''), e, 'Intake Sample', 'Alex')


def test_append_present_replace(tmp_path):
    path = tmp_path / 'board.md'
    path.write_text('# Board\n\n## Done\nOld\n\n## Open')
    line = wanted()
    assert ensure_line(path, '## Open', line, 'Intake Sample') == 'appended'
    assert path.read_text().endswith('\n' + line + '\n')
    before = path.read_bytes()
    assert ensure_line(path, '## Open', line, 'Intake Sample') == 'present'
    assert path.read_bytes() == before
    new = wanted(amount_due='$20')
    assert ensure_line(path, '## Open', new, 'Intake Sample', is_update=True, previous_line=line) == 'replaced'
    assert new in path.read_text() and line not in path.read_text()
    assert path.read_text().count('[[Intake Sample]]') == 1


@pytest.mark.parametrize('text', ['# Board\n', '## Open\n\n## Other\n'])
def test_bad_section(tmp_path, text):
    path = tmp_path / 'board.md'
    path.write_text(text)
    with pytest.raises(BoardError):
        ensure_line(path, '## Open', wanted(), 'Intake Sample')
    assert path.read_text() == text


def test_update_missing_line_is_idempotent(tmp_path):
    path = tmp_path / 'board.md'
    path.write_text('## Open\n')
    assert ensure_line(path, '## Open', wanted(), 'Intake Sample', True) == 'updated-appended'
    assert ' · UPDATED: ' in path.read_text()
    assert ensure_line(path, '## Open', wanted(), 'Intake Sample', True) == 'present'


def test_adversarial_length_and_fallback():
    line = wanted(title='[[Secret]] <script> # ignore\n' + 'x' * 500,
                  first_step='![bad] <b>step</b>')
    assert len(line) <= 300
    assert line.endswith(' · [[Intake Sample]]')
    assert line.count('[[') == 1
    assert '<' not in line and '\n' not in line
    e = Extraction(True, 'other', 'Question', 'Summary', 'Alex', 'Review this.')
    line = board_line(date(2026, 11, 6), 'Intake', 'Alex', Flags(False, False, None, ''),
                      e, 'Intake Sample', 'Alex', user_name='Sam')
    assert ' · triage with Sam: Question: proposed: Review this.' in line


def test_update_retries_on_concurrent_edit(tmp_path, monkeypatch):
    path = tmp_path / 'board.md'
    path.write_text('## Open\n' + wanted() + '\n')
    original_read = type(path).read_bytes
    calls = 0
    def racing_read(self):
        nonlocal calls
        calls += 1
        self.write_text(self.read_text() + f'Other writer {calls}\n')
        return original_read(self)
    monkeypatch.setattr(type(path), 'read_bytes', racing_read)
    with pytest.raises(BoardError, match='both update attempts'):
        ensure_line(path, '## Open', wanted(amount_due='$30'), 'Intake Sample', previous_line=wanted())
    assert 'Other writer 1' in path.read_text() and 'Other writer 2' in path.read_text()
    assert wanted() in path.read_text()


def test_single_append_write_and_updated_length(tmp_path, monkeypatch):
    import os
    path = tmp_path / 'board.md'
    path.write_text('## Open\n')
    original_write, original_open = os.write, os.open
    writes, opens = [], []
    def write(fd, data):
        writes.append(data)
        return original_write(fd, data)
    def open_file(file, flags, *args):
        opens.append(flags)
        return original_open(file, flags, *args)
    monkeypatch.setattr(os, 'write', write)
    monkeypatch.setattr(os, 'open', open_file)
    line = wanted(title='Long · title ' + 'x' * 500)
    assert ensure_line(path, '## Open', line, 'Intake Sample', True) == 'updated-appended'
    assert len(writes) == 1
    assert opens == [os.O_WRONLY | os.O_APPEND]
    added = path.read_text().splitlines()[-1]
    assert len(added) <= 300 and added.endswith(' · [[Intake Sample]]')
    assert 'UPDATED: Long · title' in added
    assert ensure_line(path, '## Open', line, 'Intake Sample', True) == 'present'


def test_retry_preserves_other_writer(tmp_path, monkeypatch):
    path = tmp_path / 'board.md'
    path.write_text('## Open\n' + wanted() + '\n')
    original_read = type(path).read_bytes
    calls = 0
    def racing_once(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            self.write_text(self.read_text() + 'Other writer\n')
        return original_read(self)
    monkeypatch.setattr(type(path), 'read_bytes', racing_once)
    assert ensure_line(path, '## Open', wanted(amount_due='$30'), 'Intake Sample', previous_line=wanted()) == 'replaced'
    assert calls == 2
    assert 'Other writer' in path.read_text()
