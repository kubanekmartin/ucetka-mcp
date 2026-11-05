"""Pracovní dny v ČR — lhůty připadající na víkend/svátek se posouvají na nejbližší pracovní den."""
from __future__ import annotations

from datetime import date, timedelta

_FIXED = {(1, 1), (5, 1), (5, 8), (7, 5), (7, 6), (9, 28), (10, 28), (11, 17), (12, 24), (12, 25), (12, 26)}


def _easter_sunday(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lday = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lday) // 451
    month = (h + lday - 7 * m + 114) // 31
    day = (h + lday - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def is_holiday(d: date) -> bool:
    easter = _easter_sunday(d.year)
    return (d.month, d.day) in _FIXED or d in (easter - timedelta(days=2), easter + timedelta(days=1))


def next_working_day(d: date) -> date:
    while d.weekday() >= 5 or is_holiday(d):
        d += timedelta(days=1)
    return d
