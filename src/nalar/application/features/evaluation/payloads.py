from nalar.application.ports.ai_contract import (
    AnswerText,
    EvalTurnIn,
    EvaluateIn,
    EvaluationPackIn,
    Kind,
    ProbeStrategy,
    RubricIn,
)
from nalar.application.ports.evaluations import EvaluationInput
from nalar.domain.labels import PROBE_MOVES

_PACK_KEYS = ("pack_version", "anchor_problem", "answer_terms", "reference_reasoning", "targets")
_MISCONCEPTION_KEYS = ("id", "concept_id", "statement", "detection_cues")


def evaluate_request(data: EvaluationInput) -> EvaluateIn:
    """AI-8: ids and text only; the pack carries no names and the turns no student identity."""
    pack = data.context_pack
    misconceptions = [
        {k: m.get(k) for k in _MISCONCEPTION_KEYS} for m in pack.get("misconceptions") or []
    ]
    return EvaluateIn(
        context_pack=EvaluationPackIn.model_validate(
            {key: pack[key] for key in _PACK_KEYS} | {"misconceptions": misconceptions}
        ),
        rubric=RubricIn.model_validate(data.rubric),
        turns=[
            EvalTurnIn(
                turn_id=t.id,
                turn_index=t.turn_index,
                kind=Kind(t.kind),
                question_text=t.question_text,
                answer_text=AnswerText(t.answer_text) if t.answer_text else None,
                move=ProbeStrategy(t.move) if t.move in PROBE_MOVES else None,
                target_concept_id=t.target_concept_id,
            )
            for t in data.turns
        ],
    )
