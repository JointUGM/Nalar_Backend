from dataclasses import dataclass
from typing import Any, Protocol

KB_QUEUE = "nalar_kb"
EVAL_QUEUE = "nalar_eval"
DEFAULT_QUEUE = "nalar_default"


@dataclass(frozen=True)
class QueueMessage:
    msg_id: int
    read_count: int
    body: dict[str, Any]


class QueueSender(Protocol):
    async def send(self, queue: str, body: dict[str, Any], delay_s: int = 0) -> int: ...


class QueueConsumer(Protocol):
    async def read(
        self, queue: str, visibility_timeout_s: int, limit: int
    ) -> list[QueueMessage]: ...

    async def delete(self, queue: str, msg_id: int) -> None: ...

    async def archive(self, queue: str, msg_id: int) -> None: ...
