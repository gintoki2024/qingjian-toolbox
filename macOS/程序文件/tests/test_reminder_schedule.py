from datetime import date, datetime, timedelta

import pytest

from qingjian.reminder_schedule import first_occurrence, next_occurrence, normalize_repeat, occurrence_count, task_occurs_on


def dt(value):
    return datetime.fromisoformat(value)


def test_once_is_strictly_after_boundary():
    first = dt("2026-10-29T09:30:00")
    assert next_occurrence(first, {"kind": "once"}, first - timedelta(seconds=1)) == first
    assert next_occurrence(first, {"kind": "once"}, first) is None


@pytest.mark.parametrize("kind,interval,after,expected", [
    ("daily", 1, "2026-10-29T09:30:00", "2026-10-30T09:30:00"),
    ("daily", 3, "2026-11-01T09:29:59", "2026-11-01T09:30:00"),
    ("daily", 3, "2026-11-01T09:30:00", "2026-11-04T09:30:00"),
    ("daily", 1, "2126-10-29T09:30:00", "2126-10-30T09:30:00"),
])
def test_daily_calendar_jump(kind, interval, after, expected):
    assert next_occurrence(dt("2026-10-29T09:30:00"), {"kind": kind, "interval": interval}, dt(after)) == dt(expected)


def test_minute_interval_skips_millions_of_ticks():
    first = dt("2000-01-01T09:00:00")
    after = dt("2099-12-31T23:59:00")
    rule = {"kind": "interval", "interval": 1, "unit": "minutes"}
    assert next_occurrence(first, rule, after) == dt("2100-01-01T00:00:00")
    assert occurrence_count(first, rule, after) == (after - first) // timedelta(minutes=1) + 1


@pytest.mark.parametrize("after,expected", [
    ("2026-01-31T09:00:00", "2026-02-28T09:00:00"),
    ("2026-02-28T09:00:00", "2026-03-31T09:00:00"),
    ("2026-03-30T09:00:00", "2026-03-31T09:00:00"),
    ("2026-04-30T09:00:00", "2026-05-31T09:00:00"),
])
def test_monthly_always_uses_original_31st(after, expected):
    assert next_occurrence(dt("2026-01-31T09:00:00"), {"kind": "monthly"}, dt(after)) == dt(expected)


def test_monthly_interval_uses_original_month_anchor():
    assert next_occurrence(dt("2026-01-31T09:00:00"), {"kind": "monthly", "interval": 2}, dt("2026-02-28T10:00:00")) == dt("2026-03-31T09:00:00")


def test_leap_year_anchor_returns_to_february_29():
    first = dt("2024-02-29T09:00:00")
    rule = {"kind": "yearly"}
    assert next_occurrence(first, rule, first) == dt("2025-02-28T09:00:00")
    assert next_occurrence(first, rule, dt("2027-03-01T00:00:00")) == dt("2028-02-29T09:00:00")
    assert next_occurrence(first, rule, dt("2099-03-01T00:00:00")) == dt("2100-02-28T09:00:00")


def test_weekdays_means_monday_friday_without_adjusted_holidays():
    first = dt("2026-10-30T09:00:00")  # Friday
    rule = {"kind": "weekdays"}
    assert next_occurrence(first, rule, first) == dt("2026-11-02T09:00:00")
    assert first_occurrence(dt("2026-10-31T09:00:00"), rule) == dt("2026-11-02T09:00:00")
    assert next_occurrence(first, {"kind": "weekdays", "interval": 2}, first) == dt("2026-11-03T09:00:00")


def test_selected_weekdays_and_two_week_anchor():
    first = dt("2026-10-28T09:00:00")  # Wednesday
    rule = {"kind": "weekly", "interval": 2, "weekdays": [4, 0, 2, 2]}
    assert normalize_repeat(rule)["weekdays"] == [0, 2, 4]
    assert next_occurrence(first, rule, first) == dt("2026-10-30T09:00:00")
    assert next_occurrence(first, rule, dt("2026-10-30T09:00:00")) == dt("2026-11-09T09:00:00")
    assert first_occurrence(first, {"kind": "weekly", "weekdays": [0]}) == dt("2026-11-02T09:00:00")


@pytest.mark.parametrize("rule", [
    {"kind": "once"}, {"kind": "daily", "interval": 3},
    {"kind": "weekdays", "interval": 2}, {"kind": "weekly", "interval": 2, "weekdays": [0, 2, 5]},
    {"kind": "monthly"}, {"kind": "yearly"}, {"kind": "interval", "unit": "hours", "interval": 7},
])
def test_arithmetic_counts_match_sequence(rule):
    first = dt("2024-01-31T09:00:00")
    current = first_occurrence(first, rule)
    for index in range(1, 70):
        if current is None:
            break
        assert occurrence_count(first, rule, current - timedelta(seconds=1)) == index - 1
        assert occurrence_count(first, rule, current) == index
        current = next_occurrence(first, rule, current)


@pytest.mark.parametrize("rule", [
    {"kind": "daily"}, {"kind": "weekly", "weekdays": [5]},
    {"kind": "monthly"}, {"kind": "yearly"}, {"kind": "interval", "unit": "days", "interval": 1000000},
])
def test_date_upper_boundary_exhausts_safely(rule):
    assert next_occurrence(datetime.max.replace(microsecond=0), rule, datetime.max) is None


@pytest.mark.parametrize("rule", [None, [], {"kind": "bad"}, {"kind": []}, {"kind": "weekly", "weekdays": []},
    {"kind": "weekly", "weekdays": [True]}, {"kind": "weekly", "weekdays": [7]},
    {"kind": "daily", "interval": 0}, {"kind": "daily", "interval": True},
    {"kind": "interval", "unit": "seconds"}, {"kind": "once", "script": "x"},
])
def test_invalid_repeat_rules_fail_clearly(rule):
    with pytest.raises(ValueError):
        normalize_repeat(rule)


def test_calendar_marks_recurrence_without_advancing_store():
    task = {"date": "2026-01-31", "remind_at": "2026-01-31T09:00:00", "repeat": {"kind": "monthly"}}
    assert task_occurs_on(task, date(2026, 2, 28))
    assert task_occurs_on(task, date(2026, 3, 31))
    assert not task_occurs_on(task, date(2026, 3, 28))
    assert task_occurs_on({"date": "2026-01-31", "remind_at": None}, date(2026, 1, 31))
