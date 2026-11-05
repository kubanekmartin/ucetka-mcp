from datetime import date

from ucetka.calendar_cz import next_working_day


def test_known_deadlines_2026() -> None:
    assert next_working_day(date(2026, 4, 25)) == date(2026, 4, 27)
    assert next_working_day(date(2026, 7, 25)) == date(2026, 7, 27)
    assert next_working_day(date(2026, 10, 25)) == date(2026, 10, 26)
    assert next_working_day(date(2026, 5, 1)) == date(2026, 5, 4)


def test_easter_monday_2026() -> None:
    assert next_working_day(date(2026, 4, 6)) == date(2026, 4, 7)
