"""Daily print queue dispatch preference in the server's timezone."""

from datetime import datetime, time, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.app.utils.local_time import local_zone


class WorkingHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    start: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    end: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")

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
