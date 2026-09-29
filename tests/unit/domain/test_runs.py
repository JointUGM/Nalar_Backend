import itertools

import pytest

from nalar.domain.labels import RunMode, RunStatus
from nalar.domain.runs import can_close, can_open_lobby, can_start, is_joinable

ALL = list(itertools.product(RunMode, RunStatus))


@pytest.mark.parametrize(("mode", "status"), ALL)
def test_open_lobby_only_from_a_scheduled_live_run(mode: RunMode, status: RunStatus) -> None:
    assert can_open_lobby(mode, status) == (mode is RunMode.live and status is RunStatus.scheduled)


@pytest.mark.parametrize(("mode", "status"), ALL)
def test_start_only_from_a_live_lobby(mode: RunMode, status: RunStatus) -> None:
    assert can_start(mode, status) == (mode is RunMode.live and status is RunStatus.lobby)


@pytest.mark.parametrize("status", list(RunStatus))
def test_close_from_lobby_or_open(status: RunStatus) -> None:
    assert can_close(status) == (status in {RunStatus.lobby, RunStatus.open})


@pytest.mark.parametrize("status", list(RunStatus))
def test_join_only_in_lobby_or_open(status: RunStatus) -> None:
    assert is_joinable(status) == (status in {RunStatus.lobby, RunStatus.open})
