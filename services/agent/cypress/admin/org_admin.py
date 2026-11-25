"""Admin: organization facts the agent repeats verbatim (board roster, board contact)."""

from fastapi import APIRouter, Depends
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import org
from ..audit import audit
from ..auth import User, require
from ..db import tx

router = APIRouter(prefix="/api/admin", tags=["admin: organization"])


class Contact(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)


class OrgIn(BaseModel):
    board_members: list[str] = Field(max_length=30)
    board_contact: Contact


@router.get("/org")
def get_org(_: User = Depends(require("admin"))):
    org.clear()
    return org.get()


@router.put("/org")
def put_org(body: OrgIn, user: User = Depends(require("admin"))):
    members = [m.strip() for m in body.board_members if m.strip()][:30]
    with tx() as conn:
        for key, value in (("board_members", members), ("board_contact", body.board_contact.model_dump())):
            conn.execute(
                """insert into org_settings (key, value, updated_by) values (%s, %s, %s)
                   on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                     updated_at = now()""", (key, Jsonb(value), user.email))
        audit(conn, user, "update", "org_settings", "board", {"board_members": members,
                                                             "board_contact": body.board_contact.model_dump()})
    org.clear()
    return org.get()


class SuggestionsIn(BaseModel):
    suggestions: list[str] = Field(max_length=8)


@router.put("/org/suggestions")
def put_suggestions(body: SuggestionsIn, user: User = Depends(require("admin"))):
    items = [s.strip()[:120] for s in body.suggestions if s.strip()][:8]
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('chat_suggestions', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                          updated_at = now()""", (Jsonb(items), user.email))
        audit(conn, user, "update", "org_settings", "chat_suggestions", {"suggestions": items})
    org.clear()
    return {"suggestions": items}


class UploadsIn(BaseModel):
    videos: bool
    max_video_mb: int = Field(ge=5, le=500)


@router.put("/org/uploads")
def put_uploads(body: UploadsIn, user: User = Depends(require("admin"))):
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('uploads', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                          updated_at = now()""", (Jsonb(body.model_dump()), user.email))
        audit(conn, user, "update", "org_settings", "uploads", body.model_dump())
    org.clear()
    return body.model_dump()


class VotesIn(BaseModel):
    rule: str = Field(pattern="^(majority|count)$")
    count: int = Field(ge=1, le=25)
    seats: int = Field(ge=1, le=25)
    days: int = Field(ge=1, le=30)
    auto_start: bool


@router.get("/org/votes")
def get_votes(_: User = Depends(require("admin"))):
    from .. import votes
    return {"settings": votes.settings(), "ready": votes.board_ready()}


@router.put("/org/votes")
def put_votes(body: VotesIn, user: User = Depends(require("admin"))):
    from .. import votes
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('board_votes', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                          updated_at = now()""", (Jsonb(body.model_dump()), user.email))
        audit(conn, user, "update", "org_settings", "board_votes", body.model_dump())
    org.clear()
    return {"settings": votes.settings(), "ready": votes.board_ready()}
