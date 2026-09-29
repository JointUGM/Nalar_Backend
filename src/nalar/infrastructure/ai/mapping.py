from collections.abc import Mapping
from typing import Final

from nalar.application.ports.ai_contract import CallStatus

CALL_STATUS_LABELS: Final[Mapping[CallStatus, str]] = {
    CallStatus.success: "succeeded",
    CallStatus.error: "failed",
}
