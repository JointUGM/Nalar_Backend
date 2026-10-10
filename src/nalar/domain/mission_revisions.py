import hashlib
import json
from collections.abc import Mapping
from typing import Any

COMPONENTS = ("anchor_problem", "reference_reasoning", "rubric", "bank")


def request_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def effective_scope(goal_changed: bool, feedback: list[dict[str, Any]]) -> list[str]:
    selected = {f["component"] for f in feedback}
    if goal_changed or selected & {"anchor_problem", "reference_reasoning"}:
        return list(COMPONENTS)
    return [c for c in COMPONENTS if c in selected]


def changed_fields(base: Mapping[str, Any], result: Mapping[str, Any]) -> list[str]:
    changed: list[str] = []
    for key in sorted(set(base) | set(result)):
        old, new = base.get(key), result.get(key)
        if old == new:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            changed.extend(f"{key}.{p}" for p in changed_fields(old, new))
        else:
            changed.append(key)
    return changed
