"""Admin: abuse protection for the public chat (App Check mode, rate limits, anonymous spend cap)."""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import org, protection
from ..audit import audit
from ..auth import User, require
from ..db import tx

router = APIRouter(prefix="/api/admin", tags=["admin: protection"])


class ProtectionIn(BaseModel):
    app_check: Literal["off", "monitor", "enforce"]
    anon_per_hour: int = Field(ge=1, le=1000)
    anon_per_day: int = Field(ge=1, le=10000)
    user_per_hour: int = Field(ge=1, le=5000)
    user_per_day: int = Field(ge=1, le=50000)
    anon_daily_usd: float = Field(ge=0, le=100)   # 0 = no cap
    off_topic_strikes: int = Field(default=3, ge=0, le=50)   # 0 = off


def _view(days: int) -> dict:
    return {"settings": protection.get(), **protection.summary(days)}


@router.get("/protection")
def get_protection(days: int = Query(7, ge=1, le=90), _: User = Depends(require("admin"))):
    return _view(days)


@router.put("/protection")
def put_protection(body: ProtectionIn, user: User = Depends(require("admin"))):
    value = body.model_dump()
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('protection', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by, updated_at = now()""",
                     (Jsonb(value), user.email))
        audit(conn, user, "update", "protection", "protection", value)
    org.clear()
    return _view(7)
