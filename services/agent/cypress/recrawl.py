"""Scheduled re-crawls (Cloud Scheduler -> POST /api/internal/recrawl, nightly).

Each source's schedule (Manual / Daily / Weekly) is set by admins on its document set. The nightly
call queues a job for every enabled source that's due, then starts the ingest job. Re-crawls are cheap:
pages the server says are unchanged (304) or that download identical are skipped before any
embedding, and only changed documents go through the content checks afterwards. A failed crawl is
retried the next night instead of waiting a week.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from .auth import require_internal
from .db import tx

log = logging.getLogger(__name__)
router = APIRouter(tags=["internal"])


# Per scheduled source: when it last succeeded, last tried, and when it's next due.
STATUS_SQL = f"""
select s.id, s.set_id, s.schedule,
       (select max(created_at) from jobs j where j.source_id = s.id and j.status = 'succeeded') as last_success,
       (select max(created_at) from jobs j where j.source_id = s.id) as last_attempt,
       exists (select 1 from jobs j where j.source_id = s.id and j.status in ('queued', 'running')) as active
from sources s join document_sets d on d.id = s.set_id
where s.enabled and d.enabled and s.schedule in ('daily', 'weekly')
"""


def _next_due(r: dict):
    """Earliest time the source becomes due (None = now)."""
    from datetime import timedelta
    # A little under a day/week, so a run that took a few minutes doesn't push the next one back a night.
    span = {"daily": timedelta(hours=20), "weekly": timedelta(days=6, hours=20)}[r["schedule"]]
    if not r["last_attempt"]:
        return None
    if not r["last_success"] or r["last_attempt"] > r["last_success"]:
        return r["last_attempt"] + timedelta(hours=20)      # latest try failed: retry the next night
    return r["last_success"] + span


def due() -> list[dict]:
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    with tx() as conn:
        rows = conn.execute(STATUS_SQL).fetchall()
    return [r for r in rows if not r["active"] and ((n := _next_due(r)) is None or n <= now)]


RUN_AT = (3, 30)   # America/Chicago; matches the Cloud Scheduler job in infra/terraform/ops.tf


def next_run(after) -> "datetime":
    """The first nightly run at or after a moment."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Chicago")
    local = after.astimezone(tz)
    run = local.replace(hour=RUN_AT[0], minute=RUN_AT[1], second=0, microsecond=0)
    if run < local:
        run += timedelta(days=1)
    return run


def next_due_by_source(set_id: int) -> dict[int, str]:
    """For the admin page: when each scheduled source in a set will next be checked (ISO time)."""
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    with tx() as conn:
        rows = conn.execute(STATUS_SQL + " and s.set_id = %s", (set_id,)).fetchall()
    return {r["id"]: next_run(max(now, _next_due(r) or now)).isoformat() for r in rows if not r["active"]}


def run() -> dict:
    sources = due()
    queued = []
    with tx() as conn:
        for s in sources:
            if conn.execute("select 1 from jobs where source_id = %s and status in ('queued', 'running')", (s["id"],)).fetchone():
                continue
            queued.append(conn.execute("insert into jobs (set_id, source_id, triggered_by) values (%s, %s, 'schedule') returning id",
                                       (s["set_id"], s["id"])).fetchone()["id"])
    if queued:
        from .ingest.trigger import start_ingest
        start_ingest()
    log.info("scheduled re-crawl queued %d job(s)", len(queued), extra={"json_fields": {"recrawl": {"jobs": queued}}})
    return {"queued": queued}


def overnight() -> dict | None:
    """What scheduled re-crawls changed in the last day, for the morning digest."""
    with tx() as conn:
        r = conn.execute(
            """select count(*)::int as jobs,
                      coalesce(sum((stats->>'new')::int), 0)::int as new,
                      coalesce(sum((stats->>'updated')::int), 0)::int as updated,
                      coalesce(sum((stats->>'gone')::int), 0)::int as gone,
                      count(*) filter (where status = 'failed')::int as failed
               from jobs where triggered_by = 'schedule' and created_at > now() - interval '1 day'
                 and status in ('succeeded', 'failed')""").fetchone()
    return r if r["jobs"] else None


@router.post("/api/internal/recrawl")
async def recrawl(request: Request):
    require_internal(request)
    return await run_in_threadpool(run)


def nightly() -> dict:
    """The 3:30 AM job: re-crawls, then retention cleanup. One failing doesn't stop the other."""
    from . import retention, sheet_sync, votes
    out = {}
    # The member sync runs first, so tonight's case files and lookups see the current list.
    for name, fn in (("member_sync", sheet_sync.nightly), ("recrawl", run), ("retention", retention.run),
                     ("votes", votes.expire_due)):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            log.exception("nightly %s failed", name)
            out[name] = {"error": type(e).__name__}
    return out


@router.post("/api/internal/nightly")
async def nightly_job(request: Request):
    require_internal(request)
    return await run_in_threadpool(nightly)
