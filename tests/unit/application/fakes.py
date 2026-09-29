from datetime import UTC, datetime, timedelta


class FakeClock:
    def __init__(self, now: datetime | None = None) -> None:
        self.current = now or datetime.now(UTC).replace(microsecond=0)

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)
