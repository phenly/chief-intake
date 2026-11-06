from dataclasses import replace
from datetime import date, datetime

from chief_intake.models import Attachment, Extraction, Flags, IntakeNote
from chief_intake.render import (expire_images_in_record, image_name, parse_front_matter,
                                 record_basename, render_record, safe_title, sanitize_field)


def example():
    note = IntakeNote('synthetic-note-1', 'synthetic-account', 'Water Bill', '',
                      datetime(2026, 11, 4), datetime(2026, 11, 4))
    extraction = Extraction(True, 'bill', 'Water Bill', 'Quarterly water bill.', 'Sam',
                            'Pay quoting invoice SAMPLE-1.', sender='Example Water',
                            amount_due='$142.17', due_date=date(2026, 11, 15),
                            reference='SAMPLE-1', contact='help@example.com',
                            note_hunch='think this is autopay', model='haiku')
    return note, extraction, Flags(False, True, date(2026, 11, 8), '')


def test_record_golden():
    note, extraction, flags = example()
    text = render_record(note, extraction, flags, [('Sample 1.jpg', None)],
                         date(2026, 11, 6), 'abc123', 'Alex')
    assert text == '''---
type: intake
source: apple-notes
note_id: synthetic-note-1
fingerprint: abc123
captured: 2026-11-04
processed: 2026-11-06
owner: Sam
kind: bill
due: 2026-11-15
penalty: null
act_by: 2026-11-08
overdue: false
images: ["Sample 1.jpg"]
model: haiku
---

# Water Bill — Example Water

**What:** Quarterly water bill.
**Amount due:** $142.17 · **Due:** 2026-11-15 · **Act by:** 2026-11-08
**Reference:** SAMPLE-1 · **Contact:** help@example.com
**Proposed first step:** Pay quoting invoice SAMPLE-1.
**Alex's note (hunch, not fact):** think this is autopay

![[Sample 1.jpg]]
'''
    metadata = parse_front_matter(text)
    assert metadata['images'] == ['Sample 1.jpg']
    assert metadata['overdue'] is False
    assert metadata['penalty'] is None


def test_sanitization_and_naming():
    assert sanitize_field('[[Secret]] <script> # ignore') == 'Secret # ignore'
    for text in ['# heading\n[[Secret]] ![x](y)<script>x</script>', '> - * # bad', '<>[[[]]]']:
        cleaned = sanitize_field(text)
        assert not any(c in cleaned for c in '\n<>[]')
        assert not cleaned.startswith(('#', '-', '>', '*'))
    assert len(sanitize_field('x' * 2000)) == 1000
    assert safe_title(' ![[A/B:C]] <b>Water</b> ') == 'ABC Water'
    assert safe_title('!!!') == 'Untitled'
    assert len(safe_title('A' * 100)) == 60
    day = date(2026, 11, 4)
    base = 'Intake 2026-11-04 Bill'
    assert record_basename(day, 'Bill', {base + '.md', base + ' 2'}) == base + ' 3'
    assert image_name(base, 2, '.pdf') == base + ' 2.pdf'


def test_pdf_problems_missing_fields_and_expiry():
    note, extraction, flags = example()
    note = replace(note, attachments=(Attachment('scan', 'scan', 'example', None, 'not synced'),))
    extraction = replace(extraction, due_date=None, amount_due=None, reference=None,
                         contact=None, note_hunch=None, penalty_date=date(2026, 11, 30))
    text = render_record(note, extraction, flags, [('old.pdf', 2), ('new.jpg', None)],
                         date(2026, 11, 6), 'abc123', 'Alex', updated=date(2026, 11, 6))
    assert 'PDF: 2 pages.' in text
    assert 'no due date printed · **Penalty:** 2026-11-30' in text
    assert 'Not read: not synced' in text
    assert "note (hunch" not in text
    assert parse_front_matter(text)['updated'] == '2026-11-06'
    expired = expire_images_in_record(text, ['old.pdf'], date(2027, 11, 6))
    assert parse_front_matter(expired)['images'] == ['new.jpg']
    assert '![[new.jpg]]' in expired
    assert '![[old.pdf]]' not in expired
    assert 'Image expired 2027-11-06 (kept 365 days).' in expired
    assert expire_images_in_record(expired, ['old.pdf'], date(2027, 11, 6)) == expired
    assert expire_images_in_record(text, ['absent.jpg'], date(2027, 11, 6)) == text


def test_unreadable_and_quoted_metadata():
    note, extraction, flags = example()
    note = replace(note, note_id='synthetic: "id"\nline')
    extraction = replace(extraction, readable=False, unreadable_reason='[[bad]] <script>',
                         owner='Sam: Alex')
    text = render_record(note, extraction, flags, [], date(2026, 11, 6), 'abc123', 'Alex')
    assert parse_front_matter(text)['note_id'] == note.note_id
    assert parse_front_matter(text)['owner'] == 'Sam: Alex'
    assert 'Not read: bad' in text
