from collections.abc import Iterable, Iterator
from typing import Any

# api-contracts.md, "Serializer leak test": hidden mission, classification, move and score fields.
FORBIDDEN_KEYS = frozenset(
    {
        "answer_state",
        "detected_misconception_id",
        "secondary_misconception_id",
        "allowed_moves",
        "prompt_strategy",
        "move",
        "move_source",
        "move_reason_code",
        "move_reason",
        "guard_result",
        "rubric",
        "question_bank",
        "answer_terms",
        "reference_reasoning",
        "context_pack",
        "probe_plan",
        "flags",
        "misconception_id",
        "misconception_ids",
        "analysis",
        "turn_quality",
    }
)
FORBIDDEN_FRAGMENTS = ("score", "level", "rationale", "evidence")
# The warm-up reply's contract field that tells the student the choice is not scored.
ALLOWED_KEYS = frozenset({"scored"})


def forbidden_keys(names: Iterable[str]) -> set[str]:
    return {
        n
        for n in names
        if n not in ALLOWED_KEYS
        and (n in FORBIDDEN_KEYS or any(f in n for f in FORBIDDEN_FRAGMENTS))
    }


def json_keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from json_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_keys(item)


def schema_property_names(schema: Any) -> Iterator[str]:
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                yield from value
            yield from schema_property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from schema_property_names(item)
