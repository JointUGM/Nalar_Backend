from collections.abc import Mapping
from typing import Any

from nalar.presentation.worker.runner import Handler


async def _not_scheduled_yet(message: dict[str, Any]) -> None:
    return None


# TODO(S19): route these to the scheduler use cases.
CRON_HANDLERS: Mapping[str, Handler] = {
    "scheduler_tick": _not_scheduled_yet,
    "release_reminders": _not_scheduled_yet,
    "weekly_digest": _not_scheduled_yet,
}
