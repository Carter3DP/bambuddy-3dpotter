"""Daily print queue dispatch preference in the server's timezone."""

from datetime import datetime, time, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.app.utils.local_time import local_zone

WORKING_HOURS_SNAPSHOT_KEY = "queue_working_hours_snapshot"


class WorkingHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    start: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    end: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    end_of_day: Literal["keep", "restore", "longest_first"] = "restore"

    @classmethod
    def from_storage(cls, value: str | None) -> "WorkingHours":
        if value:
            try:
                return cls.model_validate_json(value)
            except ValidationError:
                # A hand-edited or restored setting must not stop dispatch.
                pass
        return cls(enabled=False, start="08:30", end="16:30")

    def active_until(self, now: datetime) -> datetime | None:
        """Return the active window's deadline in UTC, if there is one."""
        if not self.enabled or self.start == self.end:
            return None

        zone = local_zone()
        today = now.astimezone(zone).date()
        start = time.fromisoformat(self.start)
        end = time.fromisoformat(self.end)
        overnight = end < start
        opening_days = (today - timedelta(days=1), today) if overnight else (today,)
        for opening_day in opening_days:
            closing_day = opening_day + timedelta(days=int(overnight))
            opens = datetime.combine(opening_day, start, zone).astimezone(timezone.utc)
            closes = datetime.combine(closing_day, end, zone).astimezone(timezone.utc)
            # Compare UTC instants so the remaining seconds account for DST.
            if opens <= now < closes:
                return closes
        return None

    def after_hours(self, now: datetime) -> bool:
        return self.enabled and self.start != self.end and self.active_until(now) is None

    def next_opening(self, now: datetime) -> datetime:
        zone = local_zone()
        today = now.astimezone(zone).date()
        start = time.fromisoformat(self.start)
        opening = datetime.combine(today, start, zone).astimezone(timezone.utc)
        if opening <= now:
            opening = datetime.combine(today + timedelta(days=1), start, zone).astimezone(timezone.utc)
        return opening

    def last_closing(self, now: datetime) -> datetime:
        zone = local_zone()
        today = now.astimezone(zone).date()
        end = time.fromisoformat(self.end)
        closing = datetime.combine(today, end, zone).astimezone(timezone.utc)
        if closing > now:
            closing = datetime.combine(today - timedelta(days=1), end, zone).astimezone(timezone.utc)
        return closing


class WorkingHoursQueueSnapshot(BaseModel):
    """The last dispatch order observed during a particular working window."""

    window: int
    ids: list[int]

    @classmethod
    def ranks(cls, raw: str | None, hours: WorkingHours, now: datetime) -> dict[int, int]:
        if not raw or hours.end_of_day != "keep" or not hours.after_hours(now):
            return {}
        try:
            snapshot = cls.model_validate_json(raw)
        except ValidationError:
            return {}
        if snapshot.window != int(hours.last_closing(now).timestamp()):
            return {}
        return {item_id: index for index, item_id in enumerate(snapshot.ids)}
