import asyncio
import logging
import signal

from nalar.application.ports.queue import DEFAULT_QUEUE, EVAL_QUEUE, KB_QUEUE, QueueConsumer
from nalar.bootstrap.container import build_container
from nalar.bootstrap.settings import Settings
from nalar.presentation.worker.runner import QueueLoop
from nalar.presentation.worker.tick import cron_handlers

log = logging.getLogger(__name__)


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    container = build_container(settings)
    try:
        consumer = await container.get(QueueConsumer)
        loops = [
            QueueLoop(KB_QUEUE, consumer, {}, settings.kb_concurrency, visibility_timeout_s=900),
            QueueLoop(
                EVAL_QUEUE, consumer, {}, settings.eval_concurrency, visibility_timeout_s=400
            ),
            QueueLoop(
                DEFAULT_QUEUE,
                consumer,
                cron_handlers(container),
                settings.default_concurrency,
                visibility_timeout_s=120,
            ),
        ]
        log.info("worker started")
        await asyncio.gather(*(loop.run(stop) for loop in loops))
    finally:
        await container.close()
        log.info("worker stopped")


async def _serve() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Railway stops a service with SIGTERM: stop reading and let in-flight handlers finish.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    await run_worker(Settings(), stop)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
