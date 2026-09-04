"""Tests for working-day arithmetic (spec §6.1, §7.2)."""

from datetime import date, datetime, timedelta, timezone

import pytest

from rlc.calendar_utils import (
    IST,
    BankCalendar,
    ist_unix,
    is_bank_weekend,
    month_key,
    nth_weekday_of_month,
    to_ist_date,
    to_ist_datetime,
)

# August 2026: 1st is a Saturday, so 8th = 2nd Saturday, 22nd = 4th Saturday.
AUG_HOLIDAYS = ["2026-08-15", "2026-08-26"]


@pytest.fixture
def cal() -> BankCalendar:
    return BankCalendar.from_dates(AUG_HOLIDAYS)


# ------------------------------------------------------------------ timezone


def test_utc_month_boundary_would_be_wrong_in_utc():
    """1 Sep 02:00 IST is 31 Aug 20:30 UTC — a different month.

    This is the bug CLAUDE.md §12 warns about. IST is UTC+5:30, so any IST time
    before 05:30 falls on the previous UTC day. A refund created just after
    midnight on the 1st would be counted into August if the period were computed
    in UTC, silently inflating the month being closed.
    """
    ts = ist_unix(date(2026, 9, 1), hour=2)
    assert to_ist_date(ts) == date(2026, 9, 1)
    assert month_key(to_ist_date(ts)) == (2026, 9)

    utc_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
    assert utc_date == date(2026, 8, 31)
    assert month_key(utc_date) == (2026, 8)


def test_late_evening_ist_stays_on_the_same_ist_day():
    ts = ist_unix(date(2026, 8, 31), hour=23, minute=30)
    assert to_ist_date(ts) == date(2026, 8, 31)
    assert month_key(to_ist_date(ts)) == (2026, 8)


def test_to_ist_datetime_is_aware_and_offset_is_530():
    dt = to_ist_datetime(ist_unix(date(2026, 8, 10), hour=9))
    assert dt.utcoffset() == timedelta(hours=5, minutes=30)
    assert dt.tzinfo is not None
    assert (dt.hour, dt.minute) == (9, 0)


def test_ist_unix_round_trips():
    d = date(2026, 6, 15)
    assert to_ist_date(ist_unix(d, hour=0, minute=0)) == d
    assert to_ist_date(ist_unix(d, hour=23, minute=59)) == d


# ----------------------------------------------------- Indian weekend rules


def test_nth_weekday_of_month():
    assert nth_weekday_of_month(date(2026, 8, 1)) == 1   # 1st Saturday
    assert nth_weekday_of_month(date(2026, 8, 8)) == 2   # 2nd Saturday
    assert nth_weekday_of_month(date(2026, 8, 15)) == 3  # 3rd Saturday
    assert nth_weekday_of_month(date(2026, 8, 22)) == 4  # 4th Saturday
    assert nth_weekday_of_month(date(2026, 8, 29)) == 5  # 5th Saturday


def test_second_and_fourth_saturdays_are_bank_weekends():
    assert is_bank_weekend(date(2026, 8, 8)) is True
    assert is_bank_weekend(date(2026, 8, 22)) is True


def test_first_third_fifth_saturdays_are_working_days():
    """The rule everyone gets wrong: a naive Mon-Fri calendar shifts every date."""
    for d in (date(2026, 8, 1), date(2026, 8, 15), date(2026, 8, 29)):
        assert is_bank_weekend(d) is False


def test_all_sundays_are_bank_weekends():
    for day in (2, 9, 16, 23, 30):
        assert is_bank_weekend(date(2026, 8, day)) is True


def test_listed_holiday_is_not_a_working_day(cal):
    # 15 Aug 2026 is Independence Day and also a 3rd Saturday.
    assert cal.is_working_day(date(2026, 8, 15)) is False
    # 26 Aug 2026 is a Wednesday holiday.
    assert date(2026, 8, 26).weekday() == 2
    assert cal.is_working_day(date(2026, 8, 26)) is False


def test_ordinary_weekday_is_a_working_day(cal):
    assert cal.is_working_day(date(2026, 8, 25)) is True


# -------------------------------------------------------- add_working_days


def test_add_zero_working_days_is_identity_even_on_a_holiday(cal):
    holiday = date(2026, 8, 15)
    assert cal.add_working_days(holiday, 0) == holiday


def test_add_working_days_skips_sunday(cal):
    friday = date(2026, 8, 21)
    assert friday.weekday() == 4
    # Sat 22 is the 4th Saturday, Sun 23 is a Sunday -> next working day is Mon 24.
    assert cal.add_working_days(friday, 1) == date(2026, 8, 24)


def test_add_working_days_skips_a_holiday(cal):
    # Tue 25 -> +1 would be Wed 26, but that is a listed holiday -> Thu 27.
    assert cal.add_working_days(date(2026, 8, 25), 1) == date(2026, 8, 27)


def test_add_working_days_counts_a_first_saturday(cal):
    # Fri 31 Jul 2026 -> Sat 1 Aug is a 1st Saturday and therefore a working day.
    assert cal.add_working_days(date(2026, 7, 31), 1) == date(2026, 8, 1)


def test_add_working_days_multi_step(cal):
    # From Thu 27 Aug: +1 Fri 28, +2 Sat 29 (5th Sat, working), +3 Mon 31.
    assert cal.add_working_days(date(2026, 8, 27), 3) == date(2026, 8, 31)


def test_add_negative_working_days_rejected(cal):
    with pytest.raises(ValueError):
        cal.add_working_days(date(2026, 8, 10), -1)


# ---------------------------------------------------- working_days_between


def test_working_days_between_is_inverse_of_add(cal):
    """The property that keeps settle_lag_wd and the maturity gate consistent."""
    for day in range(1, 32):
        start = date(2026, 8, day)
        for n in range(0, 8):
            assert cal.working_days_between(start, cal.add_working_days(start, n)) == n


def test_working_days_between_is_half_open(cal):
    assert cal.working_days_between(date(2026, 8, 3), date(2026, 8, 3)) == 0
    assert cal.working_days_between(date(2026, 8, 3), date(2026, 8, 4)) == 1


def test_working_days_between_is_signed(cal):
    a, b = date(2026, 8, 3), date(2026, 8, 7)
    assert cal.working_days_between(a, b) == -cal.working_days_between(b, a)


def test_working_days_between_excludes_holiday_and_weekend(cal):
    # Fri 14 -> Mon 17: Sat 15 is a holiday, Sun 16 is a Sunday, so only Mon counts.
    assert cal.working_days_between(date(2026, 8, 14), date(2026, 8, 17)) == 1


# ---------------------------------------------------------------- calendar


def test_working_days_in_range_matches_manual_count(cal):
    days = cal.working_days_in_range(date(2026, 8, 1), date(2026, 8, 31))
    assert date(2026, 8, 1) in days      # 1st Saturday works
    assert date(2026, 8, 8) not in days  # 2nd Saturday
    assert date(2026, 8, 15) not in days  # holiday
    assert date(2026, 8, 22) not in days  # 4th Saturday
    assert date(2026, 8, 26) not in days  # holiday
    assert date(2026, 8, 29) in days     # 5th Saturday works
    assert all(cal.is_working_day(d) for d in days)


def test_from_json_loads_the_shipped_calendar():
    from rlc.config import REPO_ROOT

    cal = BankCalendar.from_json(REPO_ROOT / "data" / "bank_holidays_IN_2026.json")
    assert date(2026, 8, 15) in cal.holidays
    assert date(2026, 1, 26) in cal.holidays
    assert date(2026, 12, 25) in cal.holidays
    # Weekends are rule-based and must not be listed in the file.
    assert date(2026, 8, 9) not in cal.holidays
