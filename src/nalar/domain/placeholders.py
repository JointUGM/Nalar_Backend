import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# The placeholders S5 may write in a narrative or a cluster explanation (INTEGRATION.md).
_PLACEHOLDER = re.compile(
    r"\{\{(total|incomplete|(?:count|resolved|mastered|developing|not_observed):[0-9a-f-]{36})\}\}"
)
_DIGIT = re.compile(r"\d")
_WORD = re.compile(r"[^\W\d_]+")


@dataclass(frozen=True)
class NarrativeLexicon:
    number_words: frozenset[str]
    count_claims: tuple[str, ...]
    score_terms: tuple[str, ...]


class PlaceholderError(ValueError):
    pass


def _phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def quantity_problems(text: str, lexicon: NarrativeLexicon, *, parent: bool = False) -> list[str]:
    """AI-4: run on the raw text, before filling inserts numerals."""
    bare = _PLACEHOLDER.sub(" ", text)
    folded = bare.casefold()
    problems: list[str] = []
    if _DIGIT.search(bare):
        problems.append("digit")
    found = set(_WORD.findall(folded)) & lexicon.number_words
    problems += sorted(f"number_word:{w}" for w in found)
    problems += [f"count_claim:{c}" for c in lexicon.count_claims if _phrase(folded, c)]
    if parent:
        problems += [f"score_term:{t}" for t in lexicon.score_terms if _phrase(folded, t)]
    if "{{" in bare or "}}" in bare:
        problems.append("malformed_placeholder")
    return problems


def counts_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, int]:
    """Placeholder token → exact count, from the ClassInsightIn stored as counts_snapshot."""
    counts = {
        "total": int(snapshot["denominator"]),
        "incomplete": int(snapshot["incomplete_count"]),
    }
    for c in snapshot["concepts"]:
        for kind in ("mastered", "developing", "not_observed"):
            counts[f"{kind}:{c['concept_id']}"] = int(c[f"{kind}_count"])
        for m in c.get("misconceptions") or []:
            counts[f"count:{m['misconception_id']}"] = int(m["count"])
            counts[f"resolved:{m['misconception_id']}"] = int(m["resolved_count"])
    return counts


def fill(template: str, counts: Mapping[str, int], lexicon: NarrativeLexicon) -> str:
    problems = quantity_problems(template, lexicon)
    problems += [f"unknown_key:{t}" for t in _PLACEHOLDER.findall(template) if t not in counts]
    if problems:
        raise PlaceholderError("; ".join(problems))
    return _PLACEHOLDER.sub(lambda m: str(counts[m.group(1)]), template)
