from nalar.application.ports.ai_contract import CallStatus
from nalar.infrastructure.ai.mapping import CALL_STATUS_LABELS


def test_every_call_status_maps_to_a_database_label() -> None:
    assert set(CALL_STATUS_LABELS) == set(CallStatus)
    assert set(CALL_STATUS_LABELS.values()) == {"succeeded", "failed"}
