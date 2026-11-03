"""Compare the calendar index with the existing pointwise schedule semantics."""
import copy
from datetime import date, datetime, timedelta

import pytest

from qingjian import calendar_index as module
from qingjian.calendar_index import calendar_marks
from qingjian.reminder_schedule import task_occurs_on

PRIORITIES = ("urgent", "important", "normal", "low")


def dates(start, count=42):
    first = date.fromisoformat(start)
    return [first + timedelta(days=index) for index in range(count)]


def task(anchor="2026-10-29T09:00:00", *, rule=None, priority="normal", note_date="2026-10-29", completed=False):
    return {"id": "example", "title": "calendar test", "date": note_date,
            "remind_at": anchor, "repeat": rule or {"kind": "once"},
            "priority": priority, "completed": completed}


def reference(tasks, days):
    return {day: tuple(priority for priority in PRIORITIES
                       if any(not item["completed"] and item["priority"] == priority and task_occurs_on(item, day)
                              for item in tasks))[:3] for day in days}


@pytest.mark.parametrize("rule", [
    {"kind": "once"}, {"kind": "daily"}, {"kind": "daily", "interval": 3},
    {"kind": "weekdays"}, {"kind": "weekdays", "interval": 3},
    {"kind": "weekly"}, {"kind": "weekly", "interval": 2, "weekdays": [0, 3, 5]},
    {"kind": "monthly"}, {"kind": "monthly", "interval": 2},
    {"kind": "yearly"}, {"kind": "yearly", "interval": 3},
    {"kind": "interval", "interval": 1, "unit": "minutes"},
    {"kind": "interval", "interval": 1001, "unit": "minutes"},
    {"kind": "interval", "interval": 17, "unit": "hours"},
    {"kind": "interval", "interval": 3, "unit": "days"},
])
def test_all_recurrences_match_pointwise_reference(rule):
    days = dates("2026-10-12")
    items = [task(rule=rule, note_date="2026-10-14")]
    assert calendar_marks(items, days) == reference(items, days)


@pytest.mark.parametrize("anchor,rule,start", [
    ("2026-01-31T09:30:00", {"kind": "monthly"}, "2026-02-20"),
    ("2024-02-29T23:59:59", {"kind": "yearly"}, "2028-02-01"),
    ("2024-02-29T09:00:00", {"kind": "yearly"}, "2100-02-01"),
    ("2024-01-31T09:00:00", {"kind": "monthly", "interval": 7}, "2026-04-20"),
    ("2026-12-31T23:59:00", {"kind": "interval", "unit": "minutes", "interval": 1}, "2026-12-24"),
    ("2026-10-30T23:59:59", {"kind": "interval", "unit": "hours", "interval": 47}, "2026-10-20"),
    ("2026-10-31T09:00:00", {"kind": "weekdays", "interval": 2}, "2026-10-26"),
    ("2026-10-28T09:00:00", {"kind": "weekly", "interval": 3, "weekdays": [0]}, "2026-10-26"),
])
def test_cross_month_leap_year_and_subdaily_boundaries(anchor, rule, start):
    days = dates(start)
    items = [task(anchor, rule=rule)]
    assert calendar_marks(items, days) == reference(items, days)


def test_priorities_completed_distinct_note_dates_and_duplicate_dates():
    items = [task(priority=priority, note_date="2026-10-28") for priority in PRIORITIES]
    items.extend([task(priority="urgent", note_date="2026-10-27", completed=True),
                  task(None, priority="low", note_date="2026-11-01")])
    days = dates("2026-10-26", 9)
    assert calendar_marks(items, days + days[::-1]) == reference(items, days)
    assert calendar_marks(items, days)[date(2026, 10, 29)] == ("urgent", "important", "normal")


def test_gapped_days_do_not_iterate_every_minute_or_every_intermediate_day(monkeypatch):
    days = [date(1900, 1, 1), date(2026, 10, 30), date(9999, 12, 31)]
    items = [task("1900-01-01T00:00:00", rule={"kind": "interval", "unit": "minutes"})]
    original, calls = module.next_occurrence, []
    def counted(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(module, "next_occurrence", counted)
    assert calendar_marks(items, days) == reference(items, days)
    assert len(calls) <= len(days)


def test_grouped_schedules_and_saturated_priority_avoid_repeated_scans(monkeypatch):
    days = dates("2026-10-01")
    items = [task("2026-01-01T09:00:00", rule={"kind": "daily"}, priority=priority)
             for priority in PRIORITIES for _ in range(500)]
    original, calls = module.next_occurrence, []
    def counted(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(module, "next_occurrence", counted)
    indexed = calendar_marks(items, days)
    assert all(values == ("urgent", "important", "normal") for values in indexed.values())
    assert len(calls) <= 42


def test_different_rules_can_saturate_same_priority_without_extra_traversal(monkeypatch):
    days = dates("2026-10-01")
    items = [task(f"2026-01-{index:02d}T09:00:00", rule={"kind": "daily"}) for index in range(1, 29)]
    original, calls = module.next_occurrence, []
    def counted(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(module, "next_occurrence", counted)
    assert calendar_marks(items, days) == reference(items, days)
    assert len(calls) <= 42


def test_inputs_are_not_mutated_and_empty_days_are_cheap():
    items = [task(rule={"kind": "weekly", "weekdays": [5, 0, 0]})]
    before = copy.deepcopy(items)
    assert calendar_marks(items, []) == {}
    result = calendar_marks(items, dates("2026-10-01"))
    result[date(2026, 10, 1)] = ("urgent",)
    assert items == before


def test_datetime_min_and_max_ranges_match_existing_rules():
    days = [date.min, date(1, 1, 2), date(9999, 12, 30), date.max]
    items = [task("0001-01-01T00:00:00", rule={"kind": "daily"}, note_date="0001-01-01"),
             task("9999-12-31T23:59:59", rule={"kind": "yearly"}, priority="urgent", note_date="9999-12-30")]
    assert calendar_marks(items, days) == reference(items, days)


def test_malformed_reminder_does_not_hide_valid_note_date():
    items = [task("bad", note_date="2026-10-28"),
             task("2026-10-29T10:00:00+08:00", note_date="2026-10-27")]
    days = dates("2026-10-26", 5)
    assert calendar_marks(items, days) == reference(items, days)
