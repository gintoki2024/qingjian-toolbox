"""Naive local-time calendar recurrence, with arithmetic jumps across gaps.

``weekdays`` means Monday to Friday, not China's adjusted working-day calendar.
Monthly/yearly rules always retain the original day as their anchor.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta

KINDS = frozenset({"once", "daily", "weekdays", "weekly", "monthly", "yearly", "interval"})


def normalize_repeat(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"kind", "interval", "unit", "weekdays"}:
        raise ValueError("重复提醒规则无效。")
    kind = value.get("kind", "once")
    interval = value.get("interval", 1)
    if not isinstance(kind, str) or kind not in KINDS or isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 1000000:
        raise ValueError("重复周期须为 1 至 1000000 的整数。")
    result = {"kind": kind, "interval": interval}
    if kind == "interval":
        unit = value.get("unit", "days")
        if not isinstance(unit, str) or unit not in {"minutes", "hours", "days"}:
            raise ValueError("间隔单位须为分钟、小时或天。")
        result["unit"] = unit
    if kind == "weekly" and "weekdays" in value:
        weekdays = value["weekdays"]
        if not isinstance(weekdays, list) or not weekdays or any(isinstance(day, bool) or not isinstance(day, int) or day not in range(7) for day in weekdays):
            raise ValueError("每周提醒须至少选择一个星期。")
        result["weekdays"] = sorted(set(weekdays))
    return result


def _datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("提醒时间须为本机日期时间，不包含时区。")
    return value


def first_occurrence(first_due: datetime, repeat: dict) -> datetime | None:
    """Return the first eligible occurrence, including the original anchor."""
    first_due = _datetime(first_due)
    rule = normalize_repeat(repeat)
    if rule["kind"] == "weekdays" and first_due.weekday() > 4:
        try:
            return first_due + timedelta(days=7 - first_due.weekday())
        except OverflowError:
            return None
    if rule["kind"] == "weekly":
        days = rule.get("weekdays", [first_due.weekday()])
        try:
            for day in days:
                if day >= first_due.weekday():
                    return first_due + timedelta(days=day - first_due.weekday())
            return first_due + timedelta(days=7 * rule["interval"] - first_due.weekday() + days[0])
        except OverflowError:
            return None
    return first_due


def _calendar_day(first: datetime, year: int, month: int) -> datetime:
    return first.replace(year=year, month=month, day=min(first.day, calendar.monthrange(year, month)[1]))


def _weekday_at(first: datetime, offset: int) -> datetime:
    weeks, remainder = divmod(offset, 5)
    days = weeks * 7 + remainder
    if first.weekday() + remainder > 4:
        days += 2
    return first + timedelta(days=days)


def _weekday_index(first: datetime, after: datetime) -> int:
    """Number of Monday-Friday dates from first through after's date, minus one."""
    days = (after.date() - first.date()).days
    weeks, remainder = divmod(days, 7)
    return weeks * 5 + sum((first.weekday() + index) % 7 < 5 for index in range(1, remainder + 1))


def next_occurrence(first_due: datetime, repeat: dict, after: datetime) -> datetime | None:
    """Find the first occurrence strictly after ``after`` without tick iteration."""
    first_due, after = _datetime(first_due), _datetime(after)
    rule = normalize_repeat(repeat)
    first = first_occurrence(first_due, rule)
    if first is None:
        return None
    if first > after:
        return first
    kind, interval = rule["kind"], rule["interval"]
    if kind == "once":
        return None
    try:
        if kind in {"daily", "interval"}:
            step = timedelta(**{rule.get("unit", "days"): interval})
            return first + step * ((after - first) // step + 1)
        if kind == "weekdays":
            index = max(0, _weekday_index(first, after))
            candidate_index = index // interval * interval
            candidate = _weekday_at(first, candidate_index)
            return candidate if candidate > after else _weekday_at(first, candidate_index + interval)
        if kind == "weekly":
            days = rule.get("weekdays", [first_due.weekday()])
            # Use ordinal dates rather than subtracting Monday near datetime.min.
            monday_ordinal = first_due.toordinal() - first_due.weekday()
            week = max(0, (after.toordinal() - monday_ordinal) // 7)
            cycle = week // interval * interval
            for shift in (0, interval):
                for day in days:
                    ordinal = monday_ordinal + (cycle + shift) * 7 + day
                    candidate = datetime.fromordinal(ordinal).replace(hour=first_due.hour, minute=first_due.minute, second=first_due.second, microsecond=first_due.microsecond)
                    if candidate >= first_due and candidate > after:
                        return candidate
            return None
        if kind == "monthly":
            distance = (after.year - first_due.year) * 12 + after.month - first_due.month
            offset = max(0, distance // interval * interval)
            for shift in (0, interval):
                month_index = first_due.year * 12 + first_due.month - 1 + offset + shift
                year, month = divmod(month_index, 12)
                candidate = _calendar_day(first_due, year, month + 1)
                if candidate > after:
                    return candidate
            return None
        if kind == "yearly":
            offset = max(0, (after.year - first_due.year) // interval * interval)
            candidate = _calendar_day(first_due, first_due.year + offset, first_due.month)
            return candidate if candidate > after else _calendar_day(first_due, first_due.year + offset + interval, first_due.month)
    except (OverflowError, ValueError):
        # datetime's upper boundary means the sequence is exhausted.
        return None
    return None


def occurrence_count(first_due: datetime, repeat: dict, through: datetime) -> int:
    """Count occurrences up to and including a boundary in constant calendar work."""
    first_due, through = _datetime(first_due), _datetime(through)
    rule = normalize_repeat(repeat)
    first = first_occurrence(first_due, rule)
    if first is None or through < first:
        return 0
    kind, interval = rule["kind"], rule["interval"]
    if kind == "once":
        return 1
    if kind in {"daily", "interval"}:
        return int((through - first) // timedelta(**{rule.get("unit", "days"): interval})) + 1
    if kind == "weekdays":
        index = _weekday_index(first, through)
        if _weekday_at(first, index) > through:
            index -= 1
        return max(0, index // interval + 1)
    if kind == "monthly":
        distance = (through.year - first_due.year) * 12 + through.month - first_due.month
        count = distance // interval + 1
        year, month = divmod(first_due.year * 12 + first_due.month - 1 + (count - 1) * interval, 12)
        return count - int(_calendar_day(first_due, year, month + 1) > through)
    if kind == "yearly":
        count = (through.year - first_due.year) // interval + 1
        return count - int(_calendar_day(first_due, first_due.year + (count - 1) * interval, first_due.month) > through)
    days = rule.get("weekdays", [first_due.weekday()])
    monday_ordinal = first_due.toordinal() - first_due.weekday()
    last_cycle = ((through.toordinal() - monday_ordinal) // 7) // interval
    count = last_cycle * len(days)
    # The first calendar week may contain weekdays before the anchor.
    count -= sum(day < first_due.weekday() for day in days)
    for day in days:
        ordinal = monday_ordinal + last_cycle * interval * 7 + day
        if ordinal > datetime.max.toordinal():
            continue
        candidate = datetime.fromordinal(ordinal).replace(hour=first_due.hour, minute=first_due.minute, second=first_due.second, microsecond=first_due.microsecond)
        if candidate <= through:
            count += 1
    return max(0, count)


def task_occurs_on(task: dict, day: date) -> bool:
    """Calendar marking: a note's date or any scheduled recurrence on that date."""
    if isinstance(day, datetime):
        day = day.date()
    if not isinstance(day, date) or not isinstance(task, dict):
        return False
    if task.get("date") == day.isoformat():
        return True
    if not task.get("remind_at"):
        return False
    try:
        anchor = datetime.fromisoformat(task["remind_at"])
        end = datetime.combine(day, time.max)
        if day == date.min:
            earlier = 0
        else:
            earlier = occurrence_count(anchor, task["repeat"], datetime.combine(day, time.min) - timedelta(microseconds=1))
        return occurrence_count(anchor, task["repeat"], end) > earlier
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
