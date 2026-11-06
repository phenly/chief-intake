"""Opt-in synthetic bake-off. Never reads a real Notes database or vault."""
import json
import os
import re
import subprocess
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

import pytest

from chief_intake.config import load
from chief_intake.extract import ClaudeCLIExtractor, extract_with_escalation
from chief_intake.media import prepare
from chief_intake.notes_source import FixtureSource

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get('CHIEF_INTAKE_LIVE') != '1',
                                                 reason='set CHIEF_INTAKE_LIVE=1 for real Claude calls')]


def test_fixture_extraction_score(tmp_path):
    fixtures = Path(__file__).parent / 'fixtures'
    cfg = replace(load(fixtures / 'config.toml'), state_dir=tmp_path / 'state', vault=tmp_path / 'vault')
    source = FixtureSource(fixtures / 'notes.jsonl', fixtures)
    keys = [json.loads(line)['key'] for line in (fixtures / 'notes.jsonl').read_text().splitlines()]
    answers = json.loads((fixtures / 'answers.json').read_text())
    extractor = ClaudeCLIExtractor(cfg)
    runner = lambda argv: subprocess.run(argv, capture_output=True, text=True)
    total = correct = 0
    digits = lambda value: re.sub(r'\D', '', value or '')
    print('\nNote             Correct / Fields')
    for note, key in zip(source.list_notes(), keys):
        parts = prepare(note.attachments, tmp_path / key, runner)
        result = extract_with_escalation(extractor, note, parts, date(2026, 11, 6), cfg)
        expected = answers[key]
        scores = []
        if 'sender' in expected:
            scores.append(expected['sender'].casefold() in (result.sender or '').casefold())
        if 'amount_due' in expected:
            scores.append(digits(expected['amount_due']) == digits(result.amount_due))
        if 'due_date' in expected:
            scores.append(expected['due_date'] in {str(result.due_date) if result.due_date else None,
                                                   str(result.penalty_date) if result.penalty_date else None})
        for field in ('owner', 'readable'):
            if field in expected:
                scores.append(getattr(result, field) == expected[field])
        if key == 'n-escrow':
            # Records first: the step must point at escrow records before any call (a fallback call is fine).
            step = result.first_step.lower()
            scores.extend(['escrow' in step, 'call' not in step or step.find('escrow') < step.find('call')])
        if key == 'n-inject':
            assert result.owner == 'Home'
            assert all('[[' not in value for value in asdict(result).values() if isinstance(value, str))
        if key == 'n-blurry':
            assert not result.readable
        correct += sum(scores)
        total += len(scores)
        print(f'{key:16} {sum(scores):2} / {len(scores):2}')
    print(f'TOTAL            {correct} / {total} ({correct / total:.1%})')
    assert correct / total >= .90
