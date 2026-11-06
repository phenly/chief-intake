"""Command line entry points and scheduled permission preflight."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import cleanup, config, pipeline, report, schedule
from .extract import ClaudeCLIExtractor
from .models import NotesControl, NotesSource
from .notes_control import AppleScriptNotesControl
from .notes_source import ParserSource
from .state import AlreadyRunning, StateDB, fingerprint, lock
from .vaultgit import GitVault


def services(cfg: config.Config) -> tuple[ParserSource, ClaudeCLIExtractor, AppleScriptNotesControl, GitVault]:
    runner = pipeline.command_runner
    return ParserSource(cfg), ClaudeCLIExtractor(cfg), AppleScriptNotesControl(runner), GitVault(cfg.vault, runner)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog='chief-intake')
    root.add_argument('--config')
    commands = root.add_subparsers(dest='command', required=True)
    for name in ('run', 'status', 'cleanup', 'doctor', 'schedule'):
        sub = commands.add_parser(name)
        sub.add_argument('--config', default=argparse.SUPPRESS)
        if name in ('run', 'cleanup'):
            sub.add_argument('--dry-run', action='store_true')
        if name == 'run':
            sub.add_argument('--preflight', action='store_true')
            sub.add_argument('--trigger', choices=('terminal', 'launchd'), default='terminal')
            sub.add_argument('--out', type=Path)
            sub.add_argument('--limit', type=int)
        if name == 'status':
            sub.add_argument('--json', action='store_true')
        if name == 'schedule':
            sub.add_argument('action', choices=('install', 'print', 'remove', 'test'))
    return root


def last_run(cfg: config.Config, scheduled: bool = False) -> dict | None:
    try:
        return json.loads((cfg.state_dir / ('last-scheduled-run.json' if scheduled else 'last-run.json')).read_text())
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        return {'success': False, 'errors': [f'Cannot read last-run.json: {exc}']}


def probe_vault(cfg: config.Config) -> None:
    # Require the configured vault and inbox to exist; a typo must not create a vault.
    with tempfile.NamedTemporaryFile(prefix='.intake-probe-', dir=cfg.inbox_path):
        pass


def preflight(cfg: config.Config, source: NotesSource, control: NotesControl, now: datetime) -> int:
    outcome = {'started': now.isoformat(), 'preflight': True, 'trigger': 'preflight', 'success': True, 'errors': []}
    for key, check in [('notes_count', lambda: len(source.list_notes())),
                       ('vault_writable', lambda: (probe_vault(cfg) or True)),
                       ('automation_count', lambda: control.folder_count(cfg.notes_folder))]:
        try:
            outcome[key] = check()
        except Exception as exc:
            outcome['success'] = False
            outcome['errors'].append(f'{key}: {exc}')
    report.write_last_run(cfg.state_dir, outcome)
    print(json.dumps(outcome))
    return 0 if outcome['success'] else 1


def status(cfg: config.Config, source: NotesSource) -> dict:
    notes = source.list_notes()
    rows = []
    db_path = cfg.state_dir / 'state.db'
    db = StateDB(db_path, read_only=True) if db_path.exists() else None
    try:
        for note in notes:
            row = db.get(note.note_id, fingerprint(note)) if db else None
            rows.append({'note_id': note.note_id, 'title': note.title,
                         'state': row.status if row else 'new',
                         'last_error': row.last_error if row else None})
    finally:
        if db:
            db.close()
    return {'notes': rows, 'last_run': last_run(cfg)}


def doctor(cfg: config.Config, source: NotesSource, control: NotesControl,
           git: GitVault, runner=pipeline.command_runner) -> int:
    checks = [
        ('config', lambda: True, 'Copy and edit config.example.toml.'),
        ('notes read', lambda: source.list_notes() is not None, 'Grant Full Disk Access to the resolved Python binary; check folder/account.'),
        ('vault writable', lambda: (probe_vault(cfg) or True), 'Create the inbox and grant Python access to the vault.'),
        ('claude on PATH', lambda: bool(shutil.which('claude')), 'Install Claude Code CLI, add it to PATH and log in.'),
        ('Notes automation', lambda: control.folder_count(cfg.notes_folder) >= 0, 'Run schedule test and approve control Notes.'),
        ('git repo', git.is_repo, 'Initialize the vault git repository.'),
        ('schedule loaded', lambda: runner(['launchctl', 'print', f'gui/{os.getuid()}/{schedule.default_label()}']).returncode == 0,
         'Run chief-intake schedule install.'),
        ('last scheduled run outcome', lambda: bool((last_run(cfg, scheduled=True) or {}).get('success')),
         'Run schedule test; inspect last-run.json and grant the resolved Python binary Full Disk Access again after upgrades.'),
    ]
    passed = True
    for name, check, hint in checks:
        try:
            ok = bool(check())
        except Exception:
            ok = False
        print(f'{"PASS" if ok else "FAIL"} {name} — {"OK" if ok else hint}')
        passed = passed and ok
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        cfg = config.load(args.config)
    except Exception as exc:
        print(f'FAIL config — {exc}', file=sys.stderr)
        return 1
    source, extractor, control, git = services(cfg)
    now = datetime.now().astimezone()
    try:
        if args.command == 'run':
            if args.preflight:
                if args.dry_run:
                    raise ValueError('--preflight cannot be combined with --dry-run')
                with lock(cfg.state_dir):
                    return preflight(cfg, source, control, now)
            result = pipeline.run(cfg, source, extractor, control, git, now,
                                  args.dry_run, args.out, args.limit, trigger=args.trigger)
            if result.already_running:
                print('already running')
            else:
                print(json.dumps({'counts': result.counts, 'errors': result.errors,
                                  'report': str(result.report_path) if result.report_path else None,
                                  'out_dir': str(result.out_dir) if result.out_dir else None}))
            return 0 if result.success else 1
        if args.command == 'status':
            value = status(cfg, source)
            if args.json:
                print(json.dumps(value, default=str))
            else:
                for note in value['notes']:
                    print(f"{note['state']}: {note['title']} ({note['note_id']})")
                print('Last run: ' + json.dumps(value['last_run']))
            return 0
        if args.command == 'doctor':
            return doctor(cfg, source, control, git, pipeline.command_runner)
        if args.command == 'cleanup':
            if args.dry_run:
                db_path = cfg.state_dir / 'state.db'
                if db_path.exists():
                    with StateDB(db_path, read_only=True) as db:
                        for item in cleanup.run(cfg, db, control, now, dry_run=True):
                            print(item.text)
                return 0
            with lock(cfg.state_dir):
                with StateDB(cfg.state_dir / 'state.db') as db:
                    paths: set[str] = set()
                    items = cleanup.run(cfg, db, control, now, args.dry_run, paths)
                    path = cleanup.write_report_tracked(cfg, db, now.date(), report.render_run_section(now, items))
                    if path:
                        paths.add(path.relative_to(cfg.vault).as_posix())
                    errors = []
                    before_holds = len(items)
                    paths = cleanup.commit_candidates(cfg, db, items)
                    if len(items) > before_holds:
                        cleanup.write_report_tracked(cfg, db, now.date(),
                                                     report.render_run_section(now, items[before_holds:]))
                        paths = cleanup.commit_candidates(cfg, db, [])
                    if cfg.git_commit and paths:
                        outcome = git.commit(sorted(paths), f'Chief Intake: cleanup (run {now:%Y-%m-%d %H:%M})')
                        if outcome in ('committed', 'nothing'):
                            db.committed_outputs(sorted(paths))
                        if outcome not in ('committed', 'nothing'):
                            errors.append(outcome)
                            item = report.ReportItem('not_committed', f'Commit failed: {outcome}')
                            items.append(item)
                            cleanup.write_report_tracked(cfg, db, now.date(), report.render_run_section(now, [item]))
                    report.write_last_run(cfg.state_dir, {'started': now.isoformat(), 'success': not errors,
                                          'items': [asdict(i) for i in items], 'errors': errors})
                    return int(bool(errors))
        runner = pipeline.command_runner
        if args.action == 'print':
            print(schedule.render_plist(cfg, sys.executable, selected_config=config.config_path(args.config).resolve()))
        elif args.action == 'install':
            print(schedule.install(cfg, runner, selected_config=config.config_path(args.config).resolve()))
            print(schedule.print_setup_instructions(sys.executable))
        elif args.action == 'remove':
            schedule.remove(cfg, runner)
        else:
            schedule.test_kickstart(cfg, runner, selected_config=config.config_path(args.config).resolve())
            print('Preflight requested. Check status or doctor for the result.')
        return 0
    except AlreadyRunning:
        print('already running')
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        if args.command in ('run', 'cleanup') and not args.dry_run:
            report.write_last_run(cfg.state_dir, {'started': now.isoformat(), 'trigger': ('preflight' if getattr(args, 'preflight', False) else getattr(args, 'trigger', 'terminal')), 'success': False, 'errors': [str(exc)]})
        return 1
