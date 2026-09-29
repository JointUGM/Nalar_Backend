from uuid import UUID


def evaluation_message(session_id: UUID) -> dict[str, str]:
    return {"kind": "evaluate_session", "session_id": str(session_id)}
