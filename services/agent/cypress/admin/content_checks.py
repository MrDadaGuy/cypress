"""Admin: content checks (documents that disagree), so admins can fix the source documents."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import changes, consistency, org
from ..audit import audit
from ..auth import User, require
from ..db import tx
from ..ingest.trigger import start_ingest

router = APIRouter(prefix="/api/admin", tags=["admin: content checks"])
admin = require("admin")


class IssueIn(BaseModel):
    status: Literal["open", "dismissed", "resolved"]
    note: str | None = Field(default=None, max_length=500)


class ChecksSettingsIn(BaseModel):
    enabled: bool
    min_similarity: float = Field(ge=0.5, le=0.99)
    max_pairs: int = Field(ge=10, le=2000)
    neighbors: int = Field(5, ge=1, le=10)
    same_document: bool = True
    rules: bool = True
    verify: bool = True
    track_changes: bool = True
    summarize_changes: bool = True
    max_changed_docs: int = Field(25, ge=1, le=200)


@router.get("/content-issues")
def list_issues(status: Literal["open", "dismissed", "resolved", "all"] = "open", _: User = Depends(admin)):
    with tx() as conn:
        rows = conn.execute(
            """select * from content_issues where (%(s)s = 'all' or status = %(s)s)
               order by case severity when 'high' then 0 when 'medium' then 1 else 2 end, found_at desc limit 200""",
            {"s": status}).fetchall()
        scans = conn.execute("select * from content_scans order by created_at desc limit 5").fetchall()
    return {"issues": rows, "counts": consistency.open_count(), "scans": scans, "settings": consistency.settings()}


@router.get("/content-issues/count")
def count(_: User = Depends(admin)):
    return consistency.open_count()


@router.put("/content-issues/{iid}")
def set_status(iid: int, body: IssueIn, user: User = Depends(admin)):
    with tx() as conn:
        r = conn.execute("""update content_issues set status = %s, note = coalesce(%s, note),
                              closed_by = case when %s = 'open' then null else %s end,
                              closed_at = case when %s = 'open' then null else now() end
                            where id = %s returning id""",
                         (body.status, body.note, body.status, user.email, body.status, iid)).fetchone()
        if not r:
            raise HTTPException(404, "Not found")
        audit(conn, user, body.status, "content_issue", iid, {"note": body.note})
    return {"ok": True}


@router.post("/content-checks/scan", status_code=202)
def scan(user: User = Depends(admin)):
    sid = consistency.request("all", user.email)
    if sid is None and not consistency.settings().get("enabled"):
        raise HTTPException(409, "Content checks are switched off")
    start_ingest()
    return {"scan_id": sid}


@router.put("/content-checks/settings")
def put_settings(body: ChecksSettingsIn, user: User = Depends(admin)):
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('content_checks', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                          updated_at = now()""", (Jsonb(body.model_dump()), user.email))
        audit(conn, user, "update", "org_settings", "content_checks", body.model_dump())
    org.clear()
    return consistency.settings()


@router.get("/content-changes")
def list_changes(status: Literal["new", "reviewed", "all"] = "new", _: User = Depends(admin)):
    return changes.listing(status)


class ChangeIn(BaseModel):
    status: Literal["new", "reviewed"]


@router.put("/content-changes/{cid}")
def set_change(cid: int, body: ChangeIn, user: User = Depends(admin)):
    with tx() as conn:
        r = conn.execute("""update document_changes set status = %s,
                              reviewed_by = case when %s = 'new' then null else %s end,
                              reviewed_at = case when %s = 'new' then null else now() end
                            where id = %s returning id""", (body.status, body.status, user.email, body.status, cid)).fetchone()
        if not r:
            raise HTTPException(404, "Not found")
    return {"ok": True}


@router.post("/content-changes/reviewed")
def all_reviewed(user: User = Depends(admin)):
    with tx() as conn:
        n = conn.execute("update document_changes set status = 'reviewed', reviewed_by = %s, reviewed_at = now() where status = 'new'",
                         (user.email,)).rowcount
    return {"reviewed": n}
