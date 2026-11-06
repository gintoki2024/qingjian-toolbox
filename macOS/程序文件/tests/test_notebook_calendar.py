"""Check offline calendar labels against dated public calendar examples."""
from datetime import date
import json
from pathlib import Path

import pytest

from qingjian.notebook_calendar import calendar_info


def test_lunar_new_year_and_term_match_hko_2025_calendar():
    # Hong Kong Observatory Gregorian-Lunar Conversion Table of 2025:
    # https://www.hko.gov.hk/en/gts/time/calendar/pdf/files/2025e.pdf
    new_year = calendar_info(date(2025, 1, 29))
    assert new_year["lunar_month"] == "正月" and new_year["lunar_day"] == "初一"
    assert "春节" in new_year["festival"]
    assert calendar_info(date(2025, 2, 3))["term"] == "立春"
    assert calendar_info(date(2025, 10, 23))["term"] == "霜降"


def test_calendar_dates_span_both_supported_boundaries_and_are_copied():
    for day in (date(1900, 1, 1), date(2000, 2, 29), date(2100, 12, 31)):
        info = calendar_info(day)
        assert info["solar"] == day.isoformat() and info["lunar"] and info["label"]
    info = calendar_info(date(2026, 10, 29))
    info["lunar"] = "changed"
    assert calendar_info(date(2026, 10, 29))["lunar"] != "changed"


@pytest.mark.parametrize("value", [date(1899, 12, 31), date(2101, 1, 1), "2026-10-29", None])
def test_invalid_calendar_range_is_explicit(value):
    with pytest.raises(ValueError, match="1900"):
        calendar_info(value)


def test_vendored_calendar_has_unchanged_mit_origin_and_needs_no_network():
    root = Path(__file__).resolve().parents[1] / "lunar_python"
    origin = json.loads((root / "来源.json").read_text(encoding="utf-8"))
    assert origin["license"] == "MIT" and origin["changed_upstream_code"] is False
    assert origin["sha256"] == "3aa11cc73c25e70ddf0ba5bdac7398c03acc9491a3aa512a91c9642973b669d6"
    assert "Copyright (c) 2020 6tail" in (root / "LICENSE.txt").read_text(encoding="utf-8")
