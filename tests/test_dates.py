"""Printed-date arithmetic, independent of models and external commands."""
from datetime import date
from pathlib import Path
import sys

import pytest

# Support the prescribed pytest entry point in an uninstalled source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chief_intake.dates import compute_flags
from chief_intake.models import Extraction, Flags

TODAY = date(2026, 11, 6)


def item(due=None, penalty=None):
    return Extraction(True, "bill", "Sample bill for Sam", "Synthetic bill.", "Sam",
                      "Check household records.", due_date=date.fromisoformat(due) if due else None,
                      penalty_date=date.fromisoformat(penalty) if penalty else None)


@pytest.mark.parametrize("due,penalty,expected", [
    ("2026-11-02", "2026-11-30", Flags(True, False, date(2026, 11, 23),
      "OVERDUE (due 2026-11-02; penalty 2026-11-30)")),
    ("2026-11-02", None, Flags(True, False, None, "OVERDUE (due 2026-11-02)")),
    ("2026-11-02", "2026-11-05", Flags(True, False, None, "OVERDUE (due 2026-11-02)")),
    ("2026-11-02", "2026-11-06", Flags(True, False, date(2026, 10, 30), "OVERDUE (due 2026-11-02)")),
    ("2026-11-06", None, Flags(False, True, date(2026, 10, 30), "")),
    (None, None, Flags(False, False, None, "")),
    ("2026-11-30", "2026-11-15", Flags(False, True, date(2026, 11, 8), "")),
    ("2026-11-30", "2026-11-02", Flags(False, False, date(2026, 11, 23), "")),
    (None, "2026-11-06", Flags(False, True, date(2026, 10, 30), "")),
    (None, "2026-11-02", Flags(False, False, None, "")),
    ("2026-11-20", None, Flags(False, True, date(2026, 11, 13), "")),
    ("2026-11-21", None, Flags(False, False, date(2026, 11, 14), "")),
])
def test_flags(due, penalty, expected):
    assert compute_flags(item(due, penalty), TODAY, 7) == expected


def test_configured_act_by_window():
    assert compute_flags(item("2027-01-02"), date(2026, 12, 30), 3) == Flags(
        False, True, date(2026, 12, 30), "")
    assert compute_flags(item("2026-11-06"), TODAY, 0).act_by == TODAY
