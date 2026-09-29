from nalar.domain.labels import RunMode, RunStatus

_JOINABLE = frozenset({RunStatus.lobby, RunStatus.open})


def can_open_lobby(mode: RunMode, status: RunStatus) -> bool:
    return mode is RunMode.live and status is RunStatus.scheduled


def can_start(mode: RunMode, status: RunStatus) -> bool:
    return mode is RunMode.live and status is RunStatus.lobby


def can_close(status: RunStatus) -> bool:
    return status in _JOINABLE


def is_joinable(status: RunStatus) -> bool:
    return status in _JOINABLE
