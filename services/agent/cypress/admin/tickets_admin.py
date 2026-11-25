"""Console: tickets for staff and the board, relayed to the Rust tickets service."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .. import cases, notify, retention, tickets, uploads
from ..auth import User, require

router = APIRouter(prefix="/api/admin", tags=["admin: tickets"])


async def _call(fn, *args, **kw):
    try:
        return await run_in_threadpool(fn, *args, **kw)
    except tickets.TicketsError as e:
        raise HTTPException(e.status if e.status < 500 else 502, e.detail) from None


@router.get("/tickets")
async def list_tickets(status: str = "open", user: User = Depends(require("staff"))):
    return await _call(tickets.list_tickets, role=user.role, actor=user.email, status=status, limit=200)


@router.get("/tickets/{tid}")
async def get_ticket(tid: int, user: User = Depends(require("staff"))):
    t = await _call(tickets.get, tid, role=user.role, actor=user.email)
    return {**t, **await run_in_threadpool(_extras, tid, user)}


def _extras(tid: int, user: User) -> dict:
    """Attachments, what retention removed, and the legal hold, alongside the ticket itself."""
    media = uploads.media_for_ticket(tid)
    gone = uploads.media_for_ticket(tid, removed=True)
    return {"photos": [m["id"] for m in media if m["kind"] == "photo"],
            "videos": [m["id"] for m in media if m["kind"] == "video"],
            "removed": {"photos": sum(m["kind"] == "photo" for m in gone), "videos": sum(m["kind"] == "video" for m in gone),
                        "at": max((m["removed_at"] for m in gone), default=None)},
            "hold": retention.hold_for(tid), "hold_all": bool(retention.get()["hold_all"]),
            "can_hold": user.has("board"),
            "keep_days": int(retention.get()["closed_ticket_days"] or 0),
            "case": cases.view(tid, user)}


class LetterIn(BaseModel):
    subject: str = Field(max_length=200)
    body: str = Field(min_length=20, max_length=5000)


class CaseDecisionIn(BaseModel):
    action: Literal["approve", "dismiss"]
    note: str | None = Field(None, max_length=500)
    letter: LetterIn | None = None          # an edited letter; omitted = approve the draft as is


class VoteIn(BaseModel):
    vote: Literal["yes", "no"]


@router.post("/tickets/{tid}/case/vote/open")
async def open_vote(tid: int, user: User = Depends(require("board"))):
    from .. import votes
    c = await run_in_threadpool(votes._case, tid)
    if not c:
        raise HTTPException(404, "This ticket has no case file")
    try:
        await run_in_threadpool(votes.open_vote, c, user.email)
    except votes.VoteError as e:
        raise HTTPException(e.status, e.detail) from None
    return {"case": await run_in_threadpool(cases.view, tid, user)}


@router.post("/tickets/{tid}/case/vote")
async def cast_vote(tid: int, body: VoteIn, user: User = Depends(require("staff"))):
    """Board members vote from the console. Only people with an active board term count."""
    from .. import votes
    person = await run_in_threadpool(lambda: votes.voter(email=user.email))
    if not person:
        raise HTTPException(403, "Only seated board members (an active board term in Users & roles) can vote")
    try:
        await run_in_threadpool(votes.cast, tid, body.vote, person, "console")
    except votes.VoteError as e:
        raise HTTPException(e.status, e.detail) from None
    return {"case": await run_in_threadpool(cases.view, tid, user)}


@router.delete("/tickets/{tid}/case/vote")
async def cancel_vote(tid: int, user: User = Depends(require("admin"))):
    from .. import votes
    try:
        await run_in_threadpool(votes.cancel, tid, user)
    except votes.VoteError as e:
        raise HTTPException(e.status, e.detail) from None
    return {"case": await run_in_threadpool(cases.view, tid, user)}


@router.put("/tickets/{tid}/case/letter")
async def edit_letter(tid: int, body: LetterIn, user: User = Depends(require("board"))):
    if user.role not in cases.settings()["full_roles"] and user.role != "admin":
        raise HTTPException(403, "Only the roles that can see full case files can edit the letter")
    try:
        return {"case": await run_in_threadpool(cases.edit_letter, tid, body.model_dump(), user)}
    except cases.CaseError as e:
        raise HTTPException(e.status, e.detail) from None


@router.put("/tickets/{tid}/case")
async def decide_case(tid: int, body: CaseDecisionIn, user: User = Depends(require("board"))):
    """The board approves the case file (and its letter, possibly edited) or dismisses it."""
    if user.role not in cases.settings()["full_roles"] and user.role != "admin":
        raise HTTPException(403, "Only the roles that can see full case files can decide on them")
    try:
        return {"case": await run_in_threadpool(cases.decide, tid, body.action, body.note,
                                                body.letter.model_dump() if body.letter else None, user)}
    except cases.CaseError as e:
        raise HTTPException(e.status, e.detail) from None


class HoldIn(BaseModel):
    on: bool
    reason: str | None = Field(None, max_length=500)


@router.put("/tickets/{tid}/hold")
async def put_hold(tid: int, body: HoldIn, user: User = Depends(require("board"))):
    """Board and admins place or release a legal hold: the ticket's attachments are kept until released."""
    try:
        hold = await run_in_threadpool(retention.set_hold, tid, body.on, body.reason, user)
    except retention.HoldError as e:
        raise HTTPException(e.status, e.detail) from None
    return {"hold": hold}


@router.get("/uploads/{upload_id}/link")
async def video_link(upload_id: str, _: User = Depends(require("staff"))):
    """Short-lived link to play a video attachment."""
    url = await run_in_threadpool(uploads.view_url, upload_id)
    if not url:
        raise HTTPException(404, "Not found")
    return {"url": url}


@router.get("/uploads/{upload_id}")
async def get_upload(upload_id: str, _: User = Depends(require("staff"))):
    data = await run_in_threadpool(uploads.read, upload_id)
    if data is None:
        raise HTTPException(404, "Not found")
    from ..db import tx
    with tx() as conn:
        ct = (conn.execute("select content_type from uploads where id = %s", (upload_id,)).fetchone() or {}).get("content_type")
    return Response(data, media_type=ct or "image/jpeg", headers={"cache-control": "private, max-age=3600"})


class TicketPatch(BaseModel):
    status: Literal["new", "acknowledged", "in_progress", "resolved", "closed"] | None = None
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    assignee: str | None = Field(None, max_length=200)
    note: str | None = Field(None, max_length=4000)


@router.patch("/tickets/{tid}")
async def patch_ticket(tid: int, body: TicketPatch, user: User = Depends(require("staff"))):
    t = await _call(tickets.update, tid, role=user.role, actor=user.email, **body.model_dump())
    await run_in_threadpool(notify.refresh, t, f"Updated by {user.email} in the console")
    return {**t, **await run_in_threadpool(_extras, tid, user)}


class Comment(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@router.post("/tickets/{tid}/comments")
async def add_comment(tid: int, body: Comment, user: User = Depends(require("staff"))):
    t = await _call(tickets.comment, tid, body.body, role=user.role, actor=user.email)
    return {**t, **await run_in_threadpool(_extras, tid, user)}
