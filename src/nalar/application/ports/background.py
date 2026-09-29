from typing import Protocol
from uuid import UUID


class BackgroundWork(Protocol):
    def warm_run(self, run_id: UUID) -> None:
        """Warm the AI for a started run off the request path; failures are logged only."""
        ...

    def run_turn_step(self, session_id: UUID, answered_turn_index: int) -> None:
        """Run the S3 turn step for an answered turn off the request path (B6)."""
        ...
