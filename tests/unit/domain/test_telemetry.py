from uuid import uuid4

from nalar.domain.telemetry import metrics_by_turn

AT = "2026-10-08T02:00:00Z"


def test_events_are_summed_per_turn_and_unattached_batches_are_ignored() -> None:
    t1, t2 = uuid4(), uuid4()
    metrics = metrics_by_turn(
        [
            (
                t1,
                [{"type": "paste", "at": AT, "value": 80}, {"type": "paste", "at": AT, "value": 5}],
            ),
            (t1, [{"type": "visibility_hidden", "at": AT, "value": 12000}]),
            (t2, [{"type": "typing", "at": AT, "value": {"duration_ms": 9000, "chars": 120}}]),
            (t2, [{"type": "disconnect", "at": AT}, {"type": "reconnect", "at": AT}]),
            (None, [{"type": "paste", "at": AT, "value": 999}]),
        ]
    )
    assert metrics[t1].chars_pasted == 85
    assert metrics[t1].paste_events == 2
    assert (metrics[t1].tab_hidden_events, metrics[t1].tab_hidden_ms) == (1, 12000)
    assert (metrics[t2].chars_typed, metrics[t2].typing_duration_ms) == (120, 9000)
    assert metrics[t2].disconnect_events == 1
    assert set(metrics) == {t1, t2}
