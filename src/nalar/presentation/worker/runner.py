import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from nalar.application.ports.queue import QueueConsumer, QueueMessage

log = logging.getLogger(__name__)

type Handler = Callable[[dict[str, Any]], Awaitable[None]]


class QueueLoop:
    """Consumes one pgmq queue. A failed message stays queued and is re-read after the
    visibility timeout; after `max_reads` reads it is archived instead of retried forever."""

    def __init__(
        self,
        queue_name: str,
        consumer: QueueConsumer,
        handlers: Mapping[str, Handler],
        concurrency: int,
        visibility_timeout_s: int,
        max_reads: int = 5,
        idle_sleep_s: float = 1.0,
    ) -> None:
        self._queue_name = queue_name
        self._consumer = consumer
        self._handlers = handlers
        self._concurrency = concurrency
        self._visibility_timeout_s = visibility_timeout_s
        self._max_reads = max_reads
        self._idle_sleep_s = idle_sleep_s

    async def run(self, stop: asyncio.Event) -> None:
        running: set[asyncio.Task[None]] = set()
        while not stop.is_set():
            free_slots = self._concurrency - len(running)
            if free_slots <= 0:
                await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                continue
            messages = await self._read(free_slots)
            for message in messages:
                task = asyncio.create_task(self._process(message))
                running.add(task)
                task.add_done_callback(running.discard)
            if not messages:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self._idle_sleep_s)
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    async def _read(self, free_slots: int) -> list[QueueMessage]:
        try:
            return await self._consumer.read(
                self._queue_name, self._visibility_timeout_s, free_slots
            )
        except Exception:
            log.exception("queue read failed", extra={"queue": self._queue_name})
            return []

    async def _process(self, message: QueueMessage) -> None:
        context = {"queue": self._queue_name, "msg_id": message.msg_id}
        try:
            await self._dispatch(message, context)
        except Exception:
            log.exception("queue ack failed; message will be re-read", extra=context)

    async def _dispatch(self, message: QueueMessage, context: dict[str, Any]) -> None:
        if message.read_count > self._max_reads:
            log.error("message exceeded max reads, archiving", extra=context)
            await self._consumer.archive(self._queue_name, message.msg_id)
            return
        kind = message.body.get("kind")
        handler = self._handlers.get(kind) if isinstance(kind, str) else None
        if handler is None:
            log.error("no handler for message kind %r, archiving", kind, extra=context)
            await self._consumer.archive(self._queue_name, message.msg_id)
            return
        try:
            await handler(message.body)
        except Exception:
            log.exception("handler failed; message will be retried", extra=context)
            return
        await self._consumer.delete(self._queue_name, message.msg_id)
