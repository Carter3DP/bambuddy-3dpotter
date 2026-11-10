from datetime import datetime, timezone

import pytest

from backend.app.schemas.working_hours import WorkingHours


@pytest.mark.parametrize(
    ("now_hour", "now_day", "closing_day"),
    [(21, 9, None), (22, 9, 10), (0, 10, 10), (6, 10, None)],
)
def test_overnight_window(monkeypatch, now_hour, now_day, closing_day):
    monkeypatch.setenv("TZ", "UTC")
    hours = WorkingHours(enabled=True, start="22:00", end="06:00")

    expected = datetime(2026, 11, closing_day, 6, tzinfo=timezone.utc) if closing_day is not None else None
    assert hours.active_until(datetime(2026, 11, now_day, now_hour, tzinfo=timezone.utc)) == expected


def test_window_uses_real_elapsed_time_across_dst(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    hours = WorkingHours(enabled=True, start="01:00", end="04:00")
    now = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)

    deadline = hours.active_until(now)
    assert deadline == datetime(2026, 3, 8, 8, tzinfo=timezone.utc)


def test_corrupt_storage_disables_priority():
    assert not WorkingHours.from_storage('{"enabled": true, "start": "99:00", "end": "16:30"}').enabled


@pytest.mark.parametrize(
    ("start", "end", "hour", "next_day", "next_hour"),
    [
        ("08:30", "16:30", 17, 10, 8),
        ("22:00", "06:00", 7, 9, 22),
    ],
)
def test_next_opening_in_server_timezone(monkeypatch, start, end, hour, next_day, next_hour):
    monkeypatch.setenv("TZ", "UTC")
    hours = WorkingHours(enabled=True, start=start, end=end)
    now = datetime(2026, 11, 9, hour, tzinfo=timezone.utc)
    minute = int(start.split(":")[1])
    assert hours.next_opening(now) == datetime(2026, 11, next_day, next_hour, minute, tzinfo=timezone.utc)
