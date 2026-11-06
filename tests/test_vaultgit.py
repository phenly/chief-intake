import subprocess

import pytest

from chief_intake.vaultgit import GitVault, ensure_ignored, intake_ignore_patterns


def runner(argv):
    return subprocess.run(argv, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        result = runner(['git', '-C', str(tmp_path), *args])
        assert result.returncode == 0, result.stderr
        return result.stdout
    git('init')
    git('config', 'user.name', 'J. Sample')
    git('config', 'user.email', 'sample@example.com')
    (tmp_path / 'record.md').write_text('initial\n')
    git('add', '--', 'record.md')
    git('commit', '-m', 'Synthetic initial record')
    return tmp_path, git, GitVault(tmp_path, runner)


def test_commit_only_and_dirty(repo):
    path, git, vault = repo
    assert vault.is_repo()
    assert not vault.dirty_vs_head('record.md')
    (path / 'record.md').write_text('unstaged change\n')
    assert vault.dirty_vs_head('record.md')
    git('add', '--', 'record.md')
    assert vault.dirty_vs_head('record.md')
    (path / 'other.md').write_text('separately staged\n')
    git('add', '--', 'other.md')
    assert vault.commit(['record.md'], 'Synthetic intake change') == 'committed'
    assert git('show', '--pretty=format:', '--name-only', 'HEAD').strip() == 'record.md'
    assert git('diff', '--cached', '--name-only').strip() == 'other.md'
    assert not vault.dirty_vs_head('record.md')
    assert vault.commit(['record.md'], 'No change') == 'nothing'
    assert vault.commit([], 'No files') == 'nothing'


def test_ignore_and_binary_refusal(repo):
    path, git, vault = repo
    patterns = intake_ignore_patterns('Inbox')
    assert len(patterns) == 12
    ignore = path / '.gitignore'
    ignore.write_text('# existing')
    assert ensure_ignored(ignore, patterns)
    assert not vault.ensure_ignored(ignore, patterns)
    assert ignore.read_text().startswith('# existing\nInbox/Intake *.jpg\n')
    inbox = path / 'Inbox'
    inbox.mkdir()
    files = []
    for ext in ('jpg', 'jpeg', 'png', 'heic', 'heif', 'pdf', 'JPG', 'JPEG', 'PNG', 'HEIC', 'HEIF', 'PDF'):
        name = f'Inbox/Intake Sample 1.{ext}'
        (path / name).write_bytes(b'synthetic media')
        files.append(name)
        with pytest.raises(ValueError, match='image or PDF'):
            vault.commit([name], 'Must refuse')
    assert vault.check_ignored(files)
    assert not vault.check_ignored(files + ['record.md'])
    with pytest.raises(ValueError, match='explicit file'):
        vault.commit(['Inbox'], 'Must refuse directory')
    assert git('diff', '--cached', '--name-only') == ''


def test_command_failures_and_safe_paths(tmp_path):
    def fail(argv):
        return subprocess.CompletedProcess(argv, 128, '', 'synthetic git failure')
    vault = GitVault(tmp_path, fail)
    assert not vault.is_repo()
    assert vault.commit(['record.md'], 'Synthetic') == 'synthetic git failure'
    with pytest.raises(RuntimeError, match='synthetic git failure'):
        vault.dirty_vs_head('record.md')
    with pytest.raises(RuntimeError, match='synthetic git failure'):
        vault.check_ignored(['sample.jpg'])
    with pytest.raises(ValueError, match='inside vault'):
        vault.commit(['../outside.md'], 'Must refuse')


def test_explicit_paths_are_literal(repo):
    path, git, vault = repo
    (path / 'sample*.md').write_text('literal requested file\n')
    (path / 'sample-other.md').write_text('unrelated file\n')
    assert vault.commit(['sample*.md'], 'Synthetic literal filename') == 'committed'
    assert git('show', '--pretty=format:', '--name-only', 'HEAD').strip() == 'sample*.md'
    assert git('ls-files', '--', 'sample-other.md') == ''
