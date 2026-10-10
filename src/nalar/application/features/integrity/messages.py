from uuid import UUID

SESSION_FLAGS_KIND = "compute_session_flags"
LIVE_SESSION_FLAGS_KIND = "compute_live_session_flags"


def session_flags_message(session_id: UUID) -> dict[str, str]:
    return {"kind": SESSION_FLAGS_KIND, "session_id": str(session_id)}


def live_session_flags_message(session_id: UUID) -> dict[str, str]:
    return {"kind": LIVE_SESSION_FLAGS_KIND, "session_id": str(session_id)}
