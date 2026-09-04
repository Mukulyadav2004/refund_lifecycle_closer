"""Working-day arithmetic on the Indian banking calendar.

Implements the timing primitives used by the closure state machine (spec §6.1)
and the timing attributes (spec §7.2).

Two rules matter and are easy to get wrong:

1. Razorpay timestamps are Unix seconds in UTC, but every period boundary in
   this project is Indian Standard Time. A refund created 31 Aug 23:30 IST is
   30 Aug 18:00 UTC, a different month. Always convert with `to_ist_date`.
2. Indian banks are closed on Sundays and on the SECOND and FOURTH Saturdays of
   each month. The first, third and fifth Saturdays are working days. A naive
   Monday-to-Friday rule is wrong and shifts every settlement date.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

_ONE_DAY = timedelta(days=1)


def to_ist_datetime(unix_seconds: int) -> datetime:
    """Convert a Razorpay Unix timestamp to an aware IST datetime."""
    return datetime.fromtimestamp(int(unix_seconds), tz=IST)


def to_ist_date(unix_seconds: int) -> date:
    """Convert a Razorpay Unix timestamp to the IST calendar date."""
    return to_ist_datetime(unix_seconds).date()


def to_unix(dt: datetime) -> int:
    """Convert an aware datetime to Unix seconds. Naive input is treated as IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return int(dt.timestamp())


def ist_unix(d: date, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    """Unix seconds for a wall-clock IST time on a given date."""
    return to_unix(datetime(d.year, d.month, d.day, hour, minute, second, tzinfo=IST))


def month_key(d: date) -> tuple[int, int]:
    """(year, month) — the unit used for the CROSS_PERIOD test."""
    return (d.year, d.month)


def nth_weekday_of_month(d: date) -> int:
    """Which occurrence of its weekday this date is (1 = first, 2 = second, ...)."""
    return (d.day - 1) // 7 + 1


def is_bank_weekend(d: date) -> bool:
    """True for Sundays and for the 2nd and 4th Saturday of the month."""
    weekday = d.weekday()  # Monday == 0
    if weekday == 6:  # Sunday
        return True
    if weekday == 5:  # Saturday
        return nth_weekday_of_month(d) in (2, 4)
    return False


@dataclass(frozen=True)
class BankCalendar:
    """Working-day calendar. `holidays` excludes weekends, which are rule-based."""

    holidays: frozenset[date]
    name: str = "IN"

    @classmethod
    def from_json(cls, path: str | Path) -> "BankCalendar":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        days = {date.fromisoformat(h["date"]) for h in payload.get("holidays", [])}
        return cls(holidays=frozenset(days), name=payload.get("jurisdiction", "IN"))

    @classmethod
    def from_dates(cls, dates: list[date] | list[str], name: str = "IN") -> "BankCalendar":
        parsed = {date.fromisoformat(d) if isinstance(d, str) else d for d in dates}
        return cls(holidays=frozenset(parsed), name=name)

    def is_working_day(self, d: date) -> bool:
        return not is_bank_weekend(d) and d not in self.holidays

    def next_working_day(self, d: date) -> date:
        """The first working day strictly after `d`."""
        cur = d + _ONE_DAY
        while not self.is_working_day(cur):
            cur += _ONE_DAY
        return cur

    def add_working_days(self, d: date, n: int) -> date:
        """Advance `n` working days from `d`.

        `n == 0` returns `d` unchanged even if `d` is itself a holiday, so that
        `add_working_days(d, 0)` is a true identity. For `n > 0` the result is
        always a working day.
        """
        if n < 0:
            raise ValueError("n must be >= 0; use working_days_between for differences")
        cur = d
        for _ in range(n):
            cur = self.next_working_day(cur)
        return cur

    def working_days_between(self, start: date, end: date) -> int:
        """Working days in the half-open interval (start, end].

        Signed: negative when `end` precedes `start`. This is the exact inverse
        of `add_working_days` for non-negative n, which the tests assert.
        """
        if end == start:
            return 0
        if end < start:
            return -self.working_days_between(end, start)
        count = 0
        cur = start
        while cur < end:
            cur += _ONE_DAY
            if self.is_working_day(cur):
                count += 1
        return count

    def working_days_in_range(self, start: date, end: date) -> list[date]:
        """Every working day in the closed interval [start, end]."""
        out: list[date] = []
        cur = start
        while cur <= end:
            if self.is_working_day(cur):
                out.append(cur)
            cur += _ONE_DAY
        return out
