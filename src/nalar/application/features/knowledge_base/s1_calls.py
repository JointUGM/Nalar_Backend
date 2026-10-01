from collections.abc import Awaitable, Callable, Sequence

from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import InvocationOut

type Record = Callable[[Sequence[InvocationOut]], Awaitable[None]]

_RETRY_ONCE = frozenset({500, 502})
_REQUEUE_CODES = frozenset({"timeout", "unreachable"})


class StepFailed(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class BuildBusy(Exception):
    pass


class BuildSuperseded(Exception):
    pass


async def call_s1[T](
    call: Callable[[str], Awaitable[AiResult[T]]], request_id: str, record: Record
) -> AiResult[T]:
    """INTEGRATION.md: 500/502 retry once then fail; 503 or no answer requeue; 422 fail.

    Invocations are recorded on every outcome (AI-6), before any result is written.
    """
    for attempt in (1, 2):
        try:
            reply = await call(f"{request_id}-{attempt}")
        except AiServiceError as error:
            await record(error.invocations)
            if error.http_status == 503 or error.code in _REQUEUE_CODES:
                raise
            if attempt == 1 and (error.http_status in _RETRY_ONCE or error.code == "bad_response"):
                continue
            raise StepFailed(error.code) from error
        await record(reply.invocations)
        return reply
    raise AssertionError("unreachable")
