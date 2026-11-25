"""Conversation review: every chat turn with its rating, for auditing answers and finding gaps.

Admin only: questions can contain residents' names, addresses and emails.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import User, require
from ..db import tx

router = APIRouter(prefix="/api/admin", tags=["admin: conversations"])

LIST_COLS = """id, public_id, at, channel, user_role, user_email, question, intent, left(answer, 280) as answer_preview,
               latency_ms, error is not null as failed, rating, feedback_comment, feedback_at"""


@router.get("/interactions")
def list_interactions(
    rating: Literal["up", "down", "rated", "unrated", "all"] = "all",
    channel: str | None = None, q: str | None = Query(None, max_length=200),
    before: int | None = None, limit: int = Query(50, le=200),
    _: User = Depends(require("admin")),
):
    where, args = [], {}
    where.append({"up": "rating = 1", "down": "rating = -1", "rated": "rating is not null",
                  "unrated": "rating is null", "all": "true"}[rating])
    if channel:
        where.append("channel = %(channel)s"); args["channel"] = channel
    else:
        where.append("channel not in ('eval', 'console')")   # test traffic: filter by channel to see it
    if q:
        where.append("(question ilike %(q)s or answer ilike %(q)s or feedback_comment ilike %(q)s)"); args["q"] = f"%{q}%"
    if before:
        where.append("id < %(before)s"); args["before"] = before
    with tx() as conn:
        rows = conn.execute(f"select {LIST_COLS} from interactions where {' and '.join(where)} "
                            f"order by id desc limit {int(limit)}", args).fetchall()
        totals = conn.execute(
            """select count(*) as turns, count(*) filter (where rating = 1) as up,
                      count(*) filter (where rating = -1) as down,
                      count(*) filter (where error is not null) as failed,
                      percentile_cont(0.5) within group (order by latency_ms) as p50_ms
               from interactions where at > now() - interval '30 days' and channel not in ('eval', 'console')""").fetchone()
    return {"items": rows, "totals_30d": totals}


@router.get("/interactions/{iid}")
def get_interaction(iid: int, _: User = Depends(require("admin"))):
    with tx() as conn:
        row = conn.execute(
            """select id, public_id, at, channel, thread_id, user_role, user_email, question, intent, answer, citations,
                      retrieval, models, latency_ms, error, rating, feedback_comment, feedback_by, feedback_at, trace_id
               from interactions where id = %s""", (iid,)).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        # The rest of the conversation, for context.
        row["thread"] = conn.execute(
            """select id, at, question, left(answer, 400) as answer, rating from interactions
               where channel = %s and thread_id = %s order by id""", (row["channel"], row["thread_id"])).fetchall()
    from ..tracing import console_url
    row["trace_url"] = console_url(row.pop("trace_id", None))
    return row
