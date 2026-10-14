from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from nalar.domain.context_pack import BankQuestion, PackMisconception


@dataclass(frozen=True)
class MentoringTurn:
    index: int
    submitted_at: datetime
    state: str | None
    misconception_id: UUID | None
    frustration: bool
    move: str | None
    question_bank_id: str | None
    target_concept_id: UUID | None


@dataclass(frozen=True)
class MentoringContext:
    session_id: UUID
    deadline_at: datetime
    turns: tuple[MentoringTurn, ...]
    misconceptions: tuple[PackMisconception, ...]
    questions: tuple[BankQuestion, ...]


@dataclass(frozen=True)
class HelpSignal:
    reason: Literal["frustration", "repeated_evasive", "persistent_misconception"]
    misconception_statement: str | None
    suggestion: str
    turn_index: int
    rank: int = 0


def help_signal(context: MentoringContext, now: datetime) -> HelpSignal | None:
    if context.deadline_at <= now or not context.turns:
        return None
    latest = context.turns[0]
    misconception = next(
        (m for m in context.misconceptions if m.id == latest.misconception_id), None
    )
    statement = misconception.statement if misconception else None
    if latest.frustration:
        return HelpSignal(
            "frustration",
            statement,
            "Bagian mana yang terasa sulit? Kita bahas satu langkah dulu.",
            latest.index,
        )
    if len(context.turns) < 2:
        return None
    previous = context.turns[1]
    if previous.index != latest.index - 1:
        return None
    if latest.state == previous.state == "evasive":
        return HelpSignal(
            "repeated_evasive",
            None,
            "Apa alasanmu memilih jawaban itu? Ceritakan satu langkah dulu.",
            latest.index,
        )
    question = next((q for q in context.questions if q.id == latest.question_bank_id), None)
    if (
        misconception is not None
        and latest.state in ("misconception", "mixed")
        and previous.state in ("misconception", "mixed")
        and latest.misconception_id == previous.misconception_id
        and latest.move == "counter_example"
        and question is not None
        and question.move == "counter_example"
        and question.misconception_id == misconception.id
        and question.concept_id == latest.target_concept_id == misconception.concept_id
    ):
        suggestion = next(
            (
                q.text
                for q in context.questions
                if q.move == "counter_example"
                and q.misconception_id == misconception.id
                and q.concept_id == misconception.concept_id
                and q.text.strip()
            ),
            "Bagaimana kamu membandingkan contoh tadi dengan penjelasanmu?",
        )
        return HelpSignal(
            "persistent_misconception", statement, f"Coba tanyakan: {suggestion}", latest.index
        )
    return None
