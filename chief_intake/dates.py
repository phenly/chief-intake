"""Deterministic date flags computed from printed dates."""
from datetime import date, timedelta

from .models import Extraction, Flags


def compute_flags(extraction: Extraction, today: date, act_by_days: int) -> Flags:
    due, penalty = extraction.due_date, extraction.penalty_date
    overdue = due is not None and due < today
    future_dates = [d for d in (due, penalty) if d is not None and d >= today]
    act_by = min(future_dates) - timedelta(days=act_by_days) if future_dates else None
    due_soon = not overdue and act_by is not None and act_by <= today + timedelta(days=7)
    label = ""
    if overdue:
        label = f"OVERDUE (due {due.isoformat()}"
        if penalty is not None and penalty > today:
            label += f"; penalty {penalty.isoformat()}"
        label += ")"
    return Flags(overdue=overdue, due_soon=due_soon, act_by=act_by, label=label)
