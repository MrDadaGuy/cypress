"""Admin: how long attachments and the lookup log are kept, and the org-wide legal hold."""

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from .. import org, retention
from ..audit import audit
from ..auth import User, require
from ..db import tx

router = APIRouter(prefix="/api/admin", tags=["admin: retention"])


class RetentionIn(BaseModel):
    unattached_hours: int = Field(ge=1, le=24 * 30)
    closed_ticket_days: int = Field(ge=0, le=3650)      # 0 = keep forever
    lookup_log_days: int = Field(ge=0, le=3650)         # 0 = keep forever
    hold_all: bool
    hold_all_reason: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def reason_when_holding(self):
        if self.hold_all and len((self.hold_all_reason or "").strip()) < 3:
            raise ValueError("Say why everything is on hold (for example, the matter or who asked).")
        if not self.hold_all:
            self.hold_all_reason = None
        return self


def _view() -> dict:
    org.clear()
    return {"settings": retention.get(), **retention.status()}


@router.get("/retention")
async def get_retention(_: User = Depends(require("admin"))):
    return await run_in_threadpool(_view)


@router.put("/retention")
async def put_retention(body: RetentionIn, user: User = Depends(require("admin"))):
    def save():
        before = retention.get()
        value = body.model_dump()
        with tx() as conn:
            conn.execute("""insert into org_settings (key, value, updated_by) values ('retention', %s, %s)
                            on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                              updated_at = now()""", (Jsonb(value), user.email))
            audit(conn, user, "update", "org_settings", "retention", value)
            if before["hold_all"] != value["hold_all"]:
                audit(conn, user, "legal_hold" if value["hold_all"] else "release_hold", "org", "all",
                      {"reason": value["hold_all_reason"] or before["hold_all_reason"]})
        org.clear()
        if before["hold_all"] != value["hold_all"]:
            retention.sync_holds()
        return _view()
    return await run_in_threadpool(save)
