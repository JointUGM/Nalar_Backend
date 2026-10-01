from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class TurnMetrics:
    chars_typed: int = 0
    chars_pasted: int = 0
    paste_events: int = 0
    typing_duration_ms: int = 0
    tab_hidden_events: int = 0
    tab_hidden_ms: int = 0
    disconnect_events: int = 0


def _apply(m: TurnMetrics, event: Mapping[str, Any]) -> TurnMetrics:
    match event["type"]:
        case "paste":
            return replace(
                m,
                chars_pasted=m.chars_pasted + int(event["value"]),
                paste_events=m.paste_events + 1,
            )
        case "visibility_hidden":
            return replace(
                m,
                tab_hidden_ms=m.tab_hidden_ms + int(event["value"]),
                tab_hidden_events=m.tab_hidden_events + 1,
            )
        case "typing":
            value = event["value"]
            return replace(
                m,
                chars_typed=m.chars_typed + int(value["chars"]),
                typing_duration_ms=m.typing_duration_ms + int(value["duration_ms"]),
            )
        case "disconnect":
            return replace(m, disconnect_events=m.disconnect_events + 1)
        case _:
            return m


def metrics_by_turn(
    batches: Iterable[tuple[UUID | None, Sequence[Mapping[str, Any]]]],
) -> dict[UUID, TurnMetrics]:
    """Batches sent without a turn_index can't be attributed, so they are skipped."""
    out: dict[UUID, TurnMetrics] = {}
    for turn_id, events in batches:
        if turn_id is None:
            continue
        metrics = out.get(turn_id, TurnMetrics())
        for event in events:
            metrics = _apply(metrics, event)
        out[turn_id] = metrics
    return out
