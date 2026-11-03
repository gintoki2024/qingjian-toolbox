"""Offline Gregorian/lunar labels for the notebook's perpetual calendar.

Uses the unmodified MIT lunar_python 1.4.8 copy shipped with this source.
Festival labels do not represent statutory days off or adjusted working days.
"""
from datetime import date, datetime
from functools import lru_cache

from lunar_python import Solar

MIN_YEAR, MAX_YEAR = 1900, 2100


@lru_cache(maxsize=8192)
def _calendar_info(day):
    solar = Solar.fromYmd(day.year, day.month, day.day)
    lunar = solar.getLunar()
    month = lunar.getMonthInChinese() + "月"
    lunar_day = lunar.getDayInChinese()
    term = lunar.getJieQi()
    festivals = tuple(dict.fromkeys(lunar.getFestivals() + solar.getFestivals()))
    festival = "、".join(festivals)
    label = term or (festivals[0] if festivals else "") or (month if lunar.getDay() == 1 else lunar_day)
    return {"solar": day.isoformat(), "weekday": "星期" + "一二三四五六日"[day.weekday()],
            "lunar": lunar.getYearInChinese() + "年" + month + lunar_day,
            "label": label if len(label) <= 4 else label[:3] + "…", "festival": festival, "term": term,
            "lunar_month": month, "lunar_day": lunar_day, "is_leap_month": lunar.getMonth() < 0}


def calendar_info(day):
    if isinstance(day, datetime):
        day = day.date()
    if not isinstance(day, date) or not MIN_YEAR <= day.year <= MAX_YEAR:
        raise ValueError(f"万年历支持 {MIN_YEAR} 至 {MAX_YEAR} 年的日期。")
    return dict(_calendar_info(day))
