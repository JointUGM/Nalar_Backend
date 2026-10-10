import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any
from uuid import UUID

from nalar.domain.telemetry import TurnMetrics


@dataclass(frozen=True)
class IntegrityConfig:
    version: int
    paste_min_chars: int
    paste_min_share: float
    paste_severity: str
    hidden_ms_per_answer: int
    hidden_events_per_session: int
    tab_severity: str
    gap_first_min_quality: int
    gap_next_max_quality: int
    gap_severity: str
    disconnect_min_jump: int
    disconnect_severity: str
    similarity_min_jaccard: float
    similarity_min_tokens: int
    similarity_severity: str


@dataclass(frozen=True)
class TurnFacts:
    turn_id: UUID
    turn_index: int
    answer: str | None
    quality: int | None
    safety_paused: bool
    metrics: TurnMetrics


@dataclass(frozen=True)
class FlagDraft:
    flag_type: str
    severity: str
    turn_id: UUID | None
    evidence: dict[str, Any]


@dataclass(frozen=True)
class AnswerFacts:
    session_id: UUID
    turn_id: UUID
    turn_index: int
    answer: str


def activity_flags(turns: Sequence[TurnFacts], cfg: IntegrityConfig) -> list[FlagDraft]:
    """Telemetry-only E1 rules; paste requires an accepted answer, tab activity does not."""
    live = sorted((t for t in turns if not t.safety_paused), key=lambda t: t.turn_index)
    answered = [t for t in live if t.answer]

    def draft(flag_type: str, severity: str, turn_id: UUID | None, **evidence: Any) -> FlagDraft:
        return FlagDraft(flag_type, severity, turn_id, {"config_version": cfg.version, **evidence})

    flags: list[FlagDraft] = []
    for t in answered:
        pasted = t.metrics.chars_pasted
        if pasted >= cfg.paste_min_chars and pasted >= cfg.paste_min_share * len(t.answer or ""):
            flags.append(
                draft(
                    "large_paste",
                    cfg.paste_severity,
                    t.turn_id,
                    chars_pasted=pasted,
                    answer_chars=len(t.answer or ""),
                )
            )

    long_hidden = [
        t.turn_index for t in live if t.metrics.tab_hidden_ms >= cfg.hidden_ms_per_answer
    ]
    hidden_events = sum(t.metrics.tab_hidden_events for t in live)
    if long_hidden or hidden_events >= cfg.hidden_events_per_session:
        flags.append(
            draft(
                "tab_switching",
                cfg.tab_severity,
                None,
                turns_over_threshold=long_hidden,
                hidden_events=hidden_events,
            )
        )

    return flags


def session_flags(turns: Sequence[TurnFacts], cfg: IntegrityConfig) -> list[FlagDraft]:
    """E1 per-session rules. No style_shift (D-S26-6); safety-paused turns skipped (D-S26-9)."""
    live = sorted((t for t in turns if not t.safety_paused), key=lambda t: t.turn_index)
    answered = [t for t in live if t.answer]

    def draft(flag_type: str, severity: str, turn_id: UUID | None, **evidence: Any) -> FlagDraft:
        return FlagDraft(flag_type, severity, turn_id, {"config_version": cfg.version, **evidence})

    flags = activity_flags(turns, cfg)

    scored = [t for t in answered if t.quality is not None]
    if (
        len(scored) >= 3
        and (scored[0].quality or 0) >= cfg.gap_first_min_quality
        and all((t.quality or 0) <= cfg.gap_next_max_quality for t in scored[1:3])
    ):
        flags.append(
            draft(
                "inconsistency_gap",
                cfg.gap_severity,
                scored[0].turn_id,
                qualities=[t.quality for t in scored[:3]],
            )
        )

    for previous, current in zip(scored, scored[1:], strict=False):
        jump = (current.quality or 0) - (previous.quality or 0)
        if current.metrics.disconnect_events > 0 and jump >= cfg.disconnect_min_jump:
            flags.append(
                draft(
                    "disconnect_pattern",
                    cfg.disconnect_severity,
                    current.turn_id,
                    quality_jump=jump,
                    disconnects=current.metrics.disconnect_events,
                )
            )
    return flags


_TOKEN = re.compile(r"\w+")


def normalized_tokens(text: str) -> frozenset[str]:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return frozenset(_TOKEN.findall("".join(c for c in folded if not unicodedata.combining(c))))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def similarity_flags(
    answers: Sequence[AnswerFacts], cfg: IntegrityConfig
) -> list[tuple[UUID, FlagDraft]]:
    """Pairs of different students' answers on the same turn index (B17)."""
    by_turn: defaultdict[int, list[tuple[AnswerFacts, frozenset[str]]]] = defaultdict(list)
    for a in answers:
        tokens = normalized_tokens(a.answer)
        if len(tokens) >= cfg.similarity_min_tokens:
            by_turn[a.turn_index].append((a, tokens))
    out: list[tuple[UUID, FlagDraft]] = []
    for group in by_turn.values():
        # ponytail: O(n²) per turn; 32 students is ~500 pairs. Use MinHash past a few hundred.
        for (a, ta), (b, tb) in combinations(group, 2):
            if a.session_id == b.session_id:
                continue
            score = jaccard(ta, tb)
            if score < cfg.similarity_min_jaccard:
                continue
            for mine, other in ((a, b), (b, a)):
                out.append(
                    (
                        mine.session_id,
                        FlagDraft(
                            "cross_student_similarity",
                            cfg.similarity_severity,
                            mine.turn_id,
                            {
                                "config_version": cfg.version,
                                "related_session_ids": [str(other.session_id)],
                                "jaccard": round(score, 3),
                            },
                        ),
                    )
                )
    return out
