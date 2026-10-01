from nalar.application.ports.ai_contract import ClassInsightIn
from nalar.application.ports.results import ClassMapInput
from nalar.domain.class_map import ClassMap

MAX_INSIGHT_CONCEPTS = 10


def class_insight_body(title: str, data: ClassMapInput, m: ClassMap) -> ClassInsightIn | None:
    """The aggregate plus names, never student ids. None when S5 would reject it (no eligible
    attempt, or more concepts than it accepts)."""
    if m.denominator == 0 or not m.concepts or len(m.concepts) > MAX_INSIGHT_CONCEPTS:
        return None
    names = dict(data.concepts)
    statements = {mid: s for mid, _, s in data.misconceptions}
    return ClassInsightIn.model_validate(
        {
            "mission_title": title,
            "denominator": m.denominator,
            "incomplete_count": m.incomplete_count,
            "concepts": [
                {
                    "concept_id": c.concept_id,
                    "name": names[c.concept_id],
                    "mastered_count": c.mastered_count,
                    "developing_count": c.developing_count,
                    "not_observed_count": c.not_observed_count,
                    "misconceptions": [
                        {
                            "misconception_id": x.misconception_id,
                            "statement": statements[x.misconception_id],
                            "count": x.count,
                            "resolved_count": x.resolved_count,
                        }
                        for x in c.misconceptions
                    ],
                }
                for c in m.concepts
            ],
        }
    )
