from datetime import datetime

from pydantic import BaseModel


class LobbyOut(BaseModel):
    status: str
    join_code: str
    lobby_opened_at: datetime


class StartedOut(BaseModel):
    status: str
    started_at: datetime
    started_count: int


class ClosedOut(BaseModel):
    status: str
    closed_at: datetime
