from collections.abc import Mapping
from typing import Any

OWN_WORDS_MESSAGE = (
    "Aktivitas menempelkan teks tercatat. "
    "Jelaskan alasanmu dengan kata-katamu sendiri. Kamu tetap bisa melanjutkan."
)

STAY_ON_PAGE_MESSAGE = (
    "Aktivitas berpindah dari halaman misi tercatat. "
    "Tetap di halaman ini saat mengerjakan. Jika perlu bantuan, beri tahu gurumu."
)


def project_student_activity_notices(
    flags: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Projects latest own open paste/tab flags to safe student reminders."""
    latest_by_kind: dict[str, dict[str, Any]] = {}
    for flag in flags:
        flag_type = flag["flag_type"]
        if flag_type == "large_paste" and "own_words" not in latest_by_kind:
            latest_by_kind["own_words"] = {
                "id": flag["id"],
                "kind": "own_words",
                "message": OWN_WORDS_MESSAGE,
                "created_at": flag["created_at"],
            }
        elif flag_type == "tab_switching" and "stay_on_page" not in latest_by_kind:
            latest_by_kind["stay_on_page"] = {
                "id": flag["id"],
                "kind": "stay_on_page",
                "message": STAY_ON_PAGE_MESSAGE,
                "created_at": flag["created_at"],
            }
    return sorted(latest_by_kind.values(), key=lambda n: (n["created_at"], n["id"]))


def project_report_flag_evidence(
    flag_type: str,
    raw_evidence: Mapping[str, Any] | None,
    turn_metrics: Mapping[int, float] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw_evidence, Mapping):
        return None
    try:
        if flag_type == "large_paste":
            paste_chars = int(raw_evidence.get("chars_pasted", raw_evidence.get("paste_chars", 0)))
            answer_chars = int(raw_evidence.get("answer_chars", 0))
            if paste_chars <= 0 or answer_chars <= 0:
                return None
            return {
                "kind": "large_paste",
                "paste_chars": paste_chars,
                "answer_chars": answer_chars,
            }
        if flag_type == "tab_switching":
            turn_indices = [
                int(x)
                for x in raw_evidence.get(
                    "turns_over_threshold", raw_evidence.get("turn_indices", [])
                )
            ]
            away_events = int(raw_evidence.get("hidden_events", raw_evidence.get("away_events", 0)))
            away_seconds_by_turn: list[dict[str, Any]] = []
            if turn_metrics:
                away_seconds_by_turn = [
                    {"turn_index": idx, "seconds": round(sec, 1)}
                    for idx, sec in sorted(turn_metrics.items())
                    if sec > 0
                ]
            elif "away_seconds_by_turn" in raw_evidence:
                away_seconds_by_turn = [
                    {"turn_index": int(item["turn_index"]), "seconds": float(item["seconds"])}
                    for item in raw_evidence["away_seconds_by_turn"]
                ]
            return {
                "kind": "tab_switching",
                "turn_indices": turn_indices,
                "away_events": away_events,
                "away_seconds_by_turn": away_seconds_by_turn,
            }
        if flag_type == "inconsistency_gap":
            qualities = [
                int(q)
                for q in raw_evidence.get("qualities", raw_evidence.get("quality_levels", []))
            ]
            if not qualities:
                return None
            return {
                "kind": "inconsistency_gap",
                "quality_levels": qualities,
            }
        if flag_type == "disconnect_pattern":
            quality_jump = int(raw_evidence.get("quality_jump", raw_evidence.get("jump", 0)))
            disconnect_count = int(
                raw_evidence.get("disconnects", raw_evidence.get("disconnect_count", 0))
            )
            return {
                "kind": "disconnect_pattern",
                "quality_jump": quality_jump,
                "disconnect_count": disconnect_count,
            }
        if flag_type == "cross_student_similarity":
            score = float(raw_evidence.get("jaccard", raw_evidence.get("similarity_score", 0.0)))
            return {
                "kind": "cross_student_similarity",
                "similarity_score": round(score, 3),
            }
    except (ValueError, TypeError, KeyError):
        return None
    return None
