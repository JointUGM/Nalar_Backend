from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Readiness:
    database: bool
    ai: bool


class ReadinessProbe(Protocol):
    async def check(self) -> Readiness:
        """Never raises: each dependency reports False when it is down or slow."""
        ...
