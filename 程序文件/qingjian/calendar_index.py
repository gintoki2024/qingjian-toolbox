"""Precompute visible calendar dots without scanning every task in paintCell."""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, time, timedelta

from .reminder_schedule import first_occurrence, next_occurrence, normalize_repeat

_PRIORITIES = ("urgent", "important", "normal", "low")
_BITS = {priority: 1 << index for index, priority in enumerate(_PRIORITIES)}
_HIGHER = {1 << index: (1 << index) - 1 for index in range(4)}


def calendar_marks(tasks, days) -> dict[date, tuple[str, ...]]:
    """Return up to three descending importance dots for every requested date.

    Note dates and one-time appointments are direct lookups. Equal recurrence
    rules share one traversal; it jumps to the next requested date after each
    match, even for minute/hour intervals. A priority already represented on a
    date needs no further schedule checks. Recurrence calculations themselves
    remain owned by ``reminder_schedule``.
    """
    requested = sorted({day.date() if isinstance(day, datetime) else day for day in days
                        if isinstance(day, date)})
    if not requested:
        return {}
    marks = dict.fromkeys(requested, 0)
    missing = {bit: set(requested) for bit in _BITS.values()}

    def mark(day, bits):
        if day not in marks:
            return
        value = marks[day] | bits
        marks[day] = value
        for bit, absent_days in missing.items():
            # Three higher dots also make a lower priority immaterial.
            if value & bit or (value & _HIGHER[bit]).bit_count() >= 3:
                absent_days.discard(day)

    groups = {}
    for task in tasks:
        if not isinstance(task, dict) or task.get("completed"):
            continue
        bit = _BITS.get(task.get("priority", "normal"), _BITS["normal"])
        note_date = task.get("date")
        if isinstance(note_date, str):
            try:
                day = date.fromisoformat(note_date)
                if day.isoformat() == note_date:
                    mark(day, bit)
            except ValueError:
                pass
        if not task.get("remind_at"):
            continue
        try:
            anchor = datetime.fromisoformat(task["remind_at"])
            if anchor.tzinfo is not None:
                continue
            rule = normalize_repeat(task["repeat"])
        except (TypeError, ValueError, KeyError):
            continue
        if rule["kind"] == "once":
            mark(anchor.date(), bit)
            continue
        key = (anchor, rule["kind"], rule["interval"], rule.get("unit"), tuple(rule.get("weekdays", ())))
        if key in groups:
            groups[key][2] |= bit
        else:
            groups[key] = [anchor, rule, bit]

    # High priorities are processed first, so lower invisible dots can saturate.
    ordered_groups = sorted(groups.values(), key=lambda group: min(index for index in range(4) if group[2] & (1 << index)))
    for anchor, rule, bits in ordered_groups:
        wanted = sorted(set().union(*(missing[bit] for bit in missing if bits & bit)))
        if not wanted:
            continue
        beginning = datetime.combine(wanted[0], time.min)
        if beginning == datetime.min:
            occurrence = first_occurrence(anchor, rule)
        else:
            occurrence = next_occurrence(anchor, rule, beginning - timedelta(microseconds=1))
        while occurrence is not None and occurrence.date() <= wanted[-1]:
            day = occurrence.date()
            mark(day, bits)
            following = bisect_right(wanted, day)
            if following == len(wanted):
                break
            # Include midnight on the next wanted date, skipping the remainder
            # of this date and every gap without enumerating subdaily repeats.
            after = datetime.combine(wanted[following], time.min) - timedelta(microseconds=1)
            occurrence = next_occurrence(anchor, rule, after)
    return {day: tuple(priority for priority in _PRIORITIES if marks[day] & _BITS[priority])[:3]
            for day in requested}
