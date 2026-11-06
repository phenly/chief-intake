import json
from datetime import date, datetime

from chief_intake.report import ReportItem, render_run_section, write_last_run, write_report


def test_render_and_create_append(tmp_path):
    items = [ReportItem('routed', 'Bill → Sam', 'Intake Sample'),
             ReportItem('couldnt', '[[Bad]] <script>\nUnreadable'),
             ReportItem('overdue', 'Bill', 'Intake Sample'),
             ReportItem('cleanup', 'Images expired')]
    section = render_run_section(datetime(2026, 11, 6, 7, 5), items)
    assert section.startswith("## Run 07:05\n\n1 processed · 1 overdue · 1 couldn't process")
    assert section.index('### Overdue') < section.index('### Routed')
    assert section.index('### Routed') < section.index("### Couldn't process")
    assert '[[Bad]]' not in section and '<script>' not in section
    path = write_report(tmp_path, date(2026, 11, 6), section)
    assert path.name == 'Intake Run 2026-11-06.md'
    assert path.read_text().startswith('# Intake Run 2026-11-06\n\n## Run 07:05')
    second = render_run_section(datetime(2026, 11, 6, 8, 10), [ReportItem('waiting', 'Sync pending')])
    assert write_report(tmp_path, date(2026, 11, 6), second) == path
    assert path.read_text().count('# Intake Run') == 1
    assert '## Run 08:10' in path.read_text()


def test_empty_and_local_journal(tmp_path):
    assert render_run_section(datetime(2026, 11, 6), []) == ''
    assert write_report(tmp_path, date(2026, 11, 6), '') is None
    assert not list(tmp_path.iterdir())
    write_last_run(tmp_path, {'processed': 1})
    write_last_run(tmp_path, {'error': 'synthetic failure'})
    assert json.loads((tmp_path / 'last-run.json').read_text()) == {'error': 'synthetic failure', 'trigger': 'terminal'}
    logs = (tmp_path / 'runs.log').read_text().splitlines()
    assert [json.loads(line) for line in logs] == [{'processed': 1, 'trigger': 'terminal'}, {'error': 'synthetic failure', 'trigger': 'terminal'}]
    assert not list(tmp_path.glob('.last-run-*'))


def test_processed_count_for_urgency_only():
    section = render_run_section(datetime(2026, 11, 6), [ReportItem('overdue', 'Sample bill')])
    assert '1 processed · 1 overdue' in section
