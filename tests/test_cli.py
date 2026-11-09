import json
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from chief_intake import cli, pipeline
from chief_intake.config import load
from chief_intake.extract import FakeExtractor
from chief_intake.notes_control import FakeNotesControl
from chief_intake.vaultgit import GitVault


class Source:
    def list_notes(self):
        return []


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = replace(load(Path(__file__).parent / 'fixtures/config.toml'),
                  vault=tmp_path / 'vault', state_dir=tmp_path / 'state')
    cfg.inbox_path.mkdir(parents=True)
    source, control = Source(), FakeNotesControl()
    runner = lambda argv: CompletedProcess(argv, 0, 'true', '')
    git = GitVault(cfg.vault, runner)
    monkeypatch.setattr(cli.config, 'load', lambda path: cfg)
    monkeypatch.setattr(cli, 'services', lambda cfg: (source, FakeExtractor({}), control, git))
    return cfg, source, control, git


def test_run_wiring(setup, monkeypatch, capsys, tmp_path):
    calls = []
    monkeypatch.setattr(cli.pipeline, 'run', lambda *args, **kw: (calls.append(args) or pipeline.RunResult()))
    assert cli.main(['run', '--dry-run', '--out', str(tmp_path / 'output'), '--limit', '2', '--config', 'synthetic.toml']) == 0
    assert calls[0][6:] == (True, tmp_path / 'output', 2)
    assert json.loads(capsys.readouterr().out)['counts'] == {}


def test_status_json_and_doctor_format(setup, monkeypatch, capsys):
    cfg, source, control, git = setup
    assert cli.main(['status', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == {'notes': [], 'last_run': None}
    monkeypatch.setattr(cli.shutil, 'which', lambda _: '/synthetic/claude')
    cli.report.write_last_run(cfg.state_dir, {'success': True, 'trigger': 'preflight', 'preflight': True})
    assert cli.doctor(cfg, source, control, git, lambda a: CompletedProcess(a, 0)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 9 and all(line.startswith('PASS ') and ' — ' in line for line in lines)
    monkeypatch.setattr(cli.shutil, 'which', lambda _: None)
    assert cli.doctor(cfg, source, control, git, lambda a: CompletedProcess(a, 1)) == 1
    assert 'FAIL claude on PATH — Install' in capsys.readouterr().out


@pytest.mark.parametrize('failure', [None, 'notes', 'vault', 'automation'])
def test_preflight_only_even_on_failure(setup, monkeypatch, failure, capsys):
    cfg, source, control, _ = setup
    cfg.state_dir.mkdir()
    def fail(*args):
        raise PermissionError('synthetic permission failure')
    if failure == 'notes':
        monkeypatch.setattr(source, 'list_notes', fail)
    elif failure == 'vault':
        monkeypatch.setattr(cli, 'probe_vault', fail)
    elif failure == 'automation':
        monkeypatch.setattr(control, 'folder_count', fail)
    monkeypatch.setattr(cli.pipeline, 'run', lambda *a: pytest.fail('preflight ran pipeline'))
    assert cli.main(['run', '--preflight']) == int(failure is not None)
    outcome = json.loads((cfg.state_dir / 'last-run.json').read_text())
    assert outcome['preflight'] and outcome['success'] == (failure is None)
    assert not (cfg.state_dir / 'preflight.request').exists()
    assert (cfg.state_dir / 'runs.log').exists()
    assert not list(cfg.inbox_path.iterdir())


@pytest.mark.parametrize('action', ['install', 'print', 'remove', 'test'])
def test_schedule_wiring(setup, monkeypatch, action, capsys):
    calls = []
    function = {'install': 'install', 'print': 'render_plist', 'remove': 'remove', 'test': 'test_kickstart'}[action]
    app = cli.launcher.AppResult(Path('/synthetic/Chief Intake.app'), rebuilt=True)
    result = {'install': cli.schedule.Installed(Path('/synthetic/job.plist'), app), 'test': app}.get(action, 'synthetic')
    monkeypatch.setattr(cli.schedule, function, lambda *a, **kw: (calls.append(a) or result))
    assert cli.main(['schedule', action]) == 0
    assert calls[0][0] == setup[0]
    if action in ('install', 'test'):
        assert 'Chief Intake' in capsys.readouterr().out


def test_cleanup_dry_run_and_config_error(setup, monkeypatch, capsys):
    assert cli.main(['cleanup', '--dry-run']) == 0
    monkeypatch.setattr(cli.config, 'load', lambda p: (_ for _ in ()).throw(ValueError('bad config')))
    assert cli.main(['doctor']) == 1
    assert capsys.readouterr().err.startswith('FAIL config — ')


def test_cleanup_dry_run_does_not_create_state(setup):
    cfg = setup[0]
    assert not cfg.state_dir.exists()
    assert cli.main(['cleanup', '--dry-run']) == 0
    assert not cfg.state_dir.exists()


def test_status_uses_read_only_state(setup, capsys):
    from chief_intake.state import StateDB
    cfg, source, _, _ = setup
    with StateDB(cfg.state_dir / 'state.db') as db:
        pass
    before = (cfg.state_dir / 'state.db').read_bytes()
    assert cli.main(['status', '--json']) == 0
    assert (cfg.state_dir / 'state.db').read_bytes() == before


def test_terminal_run_does_not_consume_preflight_request(setup, monkeypatch):
    cfg, _, _, _ = setup
    cfg.state_dir.mkdir()
    request = cfg.state_dir / 'preflight.request'
    request.touch()  # Old installations may leave this marker behind.
    calls = []
    monkeypatch.setattr(cli.pipeline, 'run', lambda *a, **kw: (calls.append(kw) or pipeline.RunResult()))
    assert cli.main(['run']) == 0
    assert calls == [{'trigger': 'terminal'}]
    assert request.exists()


def test_terminal_success_cannot_mask_scheduled_failure(setup, monkeypatch, capsys):
    cfg, source, control, git = setup
    monkeypatch.setattr(cli.shutil, 'which', lambda _: '/synthetic/claude')
    cli.report.write_last_run(cfg.state_dir, {'success': False, 'trigger': 'launchd'})
    cli.report.write_last_run(cfg.state_dir, {'success': True, 'trigger': 'terminal'})
    assert cli.doctor(cfg, source, control, git, lambda a: CompletedProcess(a, 0)) == 1
    assert 'FAIL last scheduled run outcome' in capsys.readouterr().out
    cli.report.write_last_run(cfg.state_dir, {'success': True, 'trigger': 'preflight'})
    assert cli.doctor(cfg, source, control, git, lambda a: CompletedProcess(a, 0)) == 0
