"""Public chat endpoint. Anonymous residents are welcome; a Firebase token (if sent) unlocks the
caller's role, which widens retrieval to the sets that role may see (roles.py)."""

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from . import protection
from .agent import service
from .auth import User, current_user

router = APIRouter(tags=["chat"])


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=service.MAX_CHARS)
    thread_id: str | None = Field(None, pattern=r"^[a-f0-9]{32}$")
    attachments: list[str] = Field(default_factory=list, max_length=4)   # upload ids from /api/chat/upload


class FeedbackIn(BaseModel):
    turn_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    rating: Literal[1, -1] | None = None
    comment: str | None = Field(None, max_length=1000)


def optional_user(request: Request) -> User | None:
    if not request.headers.get("authorization"):
        return None
    return current_user(request)  # raises 401 for a bad token rather than silently downgrading


def client_ip(request: Request) -> str:
    # Firebase Hosting and Cloud Run put the original client first in X-Forwarded-For.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def limited(e: service.RateLimited, user: User | None, what: str = "chat limit") -> HTTPException | protection.Blocked:
    """Visitors who hit a limit are told signing in raises it; members just wait."""
    if user is None:
        return protection.Blocked(429, f"You've reached the {what} for visitors this {e}. {protection.SIGN_IN}", f"rate_{e}")
    return HTTPException(429, f"You've reached the {what} for this {e}. Please try again later.")


async def guard(request: Request, user: User | None, endpoint: str, key: str, kind: str, *, spend: bool = False) -> None:
    """Protection checks, then the rate limit, before any paid work."""
    await run_in_threadpool(protection.gate, request, user, endpoint, key, spend=spend)
    try:
        await run_in_threadpool(service.check_rate, key, kind)
    except service.RateLimited as e:
        raise limited(e, user, "chat limit" if endpoint == "chat" else "upload limit") from None


@router.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    user = optional_user(request)
    key = f"chat:ip:{client_ip(request)}"
    await run_in_threadpool(protection.gate, request, user, "chat", key, spend=True)
    try:
        r = await run_in_threadpool(
            service.run_turn, body.message, thread_id=body.thread_id,
            role=user.role if user else None, email=user.email if user else None,
            uid=user.uid if user else None, channel="web", client_key=f"ip:{client_ip(request)}")
    except service.RateLimited as e:
        raise limited(e, user) from None
    if user is None and r.get("intent") == "off_topic":
        await run_in_threadpool(protection.strike, key)
    return r


@router.post("/api/chat/feedback")
async def feedback(body: FeedbackIn, request: Request):
    user = optional_user(request)
    try:
        await run_in_threadpool(service.check_rate, f"feedback:{user.uid if user else client_ip(request)}",
                                "user" if user else "anon")
    except service.RateLimited:
        raise HTTPException(429, "Too much feedback for now. Thank you, though!") from None
    if body.rating is None and not body.comment:
        raise HTTPException(422, "Send a rating or a comment")
    ok = await run_in_threadpool(service.record_feedback, body.turn_id, body.rating, body.comment,
                                 user.email if user else "anonymous")
    if not ok:
        raise HTTPException(404, "That answer wasn't found")
    return {"ok": True}


@router.post("/api/chat/stream")
async def chat_stream(body: ChatIn, request: Request):
    """Server-sent events: status updates, answer tokens as they're written, then a final "done"
    event with citations and the turn id (for feedback)."""
    user = optional_user(request)
    key = f"chat:{user.uid if user else 'ip:' + client_ip(request)}"
    await guard(request, user, "chat", key, "user" if user else "anon", spend=True)

    def events():
        for ev in service.stream_turn(body.message, thread_id=body.thread_id, role=user.role if user else None,
                                      email=user.email if user else None, uid=user.uid if user else None,
                                      channel="web", attachments=body.attachments):
            if user is None and ev.get("type") == "done" and ev.get("intent") == "off_topic":
                protection.strike(key)
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache, no-transform", "x-accel-buffering": "no"})


@router.post("/api/chat/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    """One photo for an issue report. Returns an upload id to send with the next chat message."""
    from . import uploads
    user = optional_user(request)
    owner = user.uid if user else f"ip:{client_ip(request)}"
    await guard(request, user, "upload", f"upload:{owner}", "user" if user else "anon")
    data = await file.read(uploads.MAX_BYTES + 1)
    try:
        upload_id = await run_in_threadpool(uploads.save, data, channel="web", owner=owner)
    except uploads.UploadError as e:
        raise HTTPException(422, str(e)) from None
    return {"upload_id": upload_id}


class VideoIn(BaseModel):
    content_type: str = Field(max_length=100)
    bytes: int = Field(gt=0)


@router.post("/api/chat/upload-video")
async def upload_video(body: VideoIn, request: Request):
    """Reserve a video upload: returns a one-time signed link the browser PUTs the file to."""
    from . import uploads
    user = optional_user(request)
    owner = user.uid if user else f"ip:{client_ip(request)}"
    await guard(request, user, "upload", f"upload:{owner}", "user" if user else "anon")
    try:
        return await run_in_threadpool(uploads.start_video, body.content_type, body.bytes, channel="web", owner=owner)
    except uploads.UploadError as e:
        raise HTTPException(422, str(e)) from None


@router.post("/api/chat/upload/{upload_id}/done")
async def upload_done(upload_id: str):
    """The browser finished PUTting a video; confirm it so it can be attached to the next message."""
    from . import uploads
    try:
        return {"upload_id": await run_in_threadpool(uploads.finish_video, upload_id)}
    except uploads.UploadError as e:
        raise HTTPException(422, str(e)) from None


@router.put("/api/chat/upload/{upload_id}/local")
async def upload_local(upload_id: str, request: Request):
    """Dev only: stand-in for the bucket's signed upload link."""
    from . import uploads
    from .config import settings
    if settings.uploads_bucket:
        raise HTTPException(404)
    try:
        await run_in_threadpool(uploads.save_local_video, upload_id, await request.body())
    except uploads.UploadError as e:
        raise HTTPException(422, str(e)) from None
    return {"ok": True}


@router.get("/api/chat/pending/{pid}")
async def pending(pid: str):
    """What the sign-in card shows for a waiting action (kind and title only)."""
    from . import members
    if not re.fullmatch(r"[0-9a-f-]{36}", pid):
        raise HTTPException(404, "Not found")
    s = await run_in_threadpool(members.pending_summary, pid)
    if not s:
        raise HTTPException(404, "Not found")
    return s


@router.post("/api/chat/pending/{pid}/complete")
async def complete_pending(pid: str, request: Request):
    """After sign-in: run the waiting action (e.g. file the drafted report) for a verified member."""
    from . import members
    if not re.fullmatch(r"[0-9a-f-]{36}", pid):
        raise HTTPException(404, "Not found")
    user = current_user(request)
    try:
        await run_in_threadpool(service.check_rate, f"pending:{user.uid}", "user")
        return await run_in_threadpool(members.complete_pending, pid, role=user.role, email=user.email, uid=user.uid)
    except service.RateLimited:
        raise HTTPException(429, "Too many attempts. Try again later.") from None
    except members.PendingError as e:
        raise HTTPException(e.status, e.detail) from None


@router.get("/api/chat/suggestions")
async def suggestions():
    """Suggested questions for the welcome screen (public; admins edit them in Board & contacts)."""
    from . import org, uploads
    o = await run_in_threadpool(org.get)
    u = {**uploads.UPLOAD_DEFAULTS, **(o.get("uploads") or {})}
    return {"suggestions": o.get("chat_suggestions") or [], "uploads": {"videos": bool(u["videos"]), "max_video_mb": int(u["max_video_mb"])}}
