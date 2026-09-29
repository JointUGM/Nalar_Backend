from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class TurnTiming:
    recovery_after: timedelta
