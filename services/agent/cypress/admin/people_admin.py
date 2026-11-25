"""Admin: people (roles + Slack identity) and groups.

Admin only, except /assignees (names for the ticket assignee picker, staff and up). Every change
is audited. The last active admin can't be removed, deactivated or demoted.
"""

import logging
import re
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator, model_validator

from .. import org, people, roles
from ..audit import audit
from ..auth import User, require
from ..config import settings
from ..db import tx

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin: people"])

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CATEGORIES = ("maintenance", "safety", "rules", "pool", "grounds", "other")
SLACK_ID = re.compile(r"^[UW][A-Z0-9]{6,15}$")


class TermIn(BaseModel):
    title: str = Field(default="Director", min_length=2, max_length=60)
    seat: str | None = Field(default=None, max_length=40)
    how: str = Field(default="elected", pattern="^(elected|appointed)$")
    start_date: date
    end_date: date
    ended_on: date | None = None
    end_reason: str | None = Field(default=None, pattern="^(resigned|removed|other)$")
    notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _dates(self):
        if self.end_date < self.start_date:
            raise ValueError("a term can't end before it starts")
        if self.ended_on and self.ended_on < self.start_date:
            raise ValueError("left-early date is before the term starts")
        if bool(self.ended_on) != bool(self.end_reason):
            raise ValueError("give both the date they left and the reason, or neither")
        return self


class PersonIn(BaseModel):
    email: str = Field(max_length=200)
    name: str | None = Field(default=None, max_length=120)
    role: str | None = None
    active: bool = True
    slack_user_id: str | None = Field(default=None, max_length=20)
    notes: str | None = Field(default=None, max_length=1000)
    phone: str | None = Field(default=None, max_length=40)
    responsibilities: str | None = Field(default=None, max_length=2000)
    groups: list[int] = Field(default_factory=list, max_length=20)
    terms: list[TermIn] = Field(default_factory=list, max_length=20)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL.match(v):
            raise ValueError("not an email address")
        return v

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v in (None, "", "none"):
            return None
        if v not in roles.ROLES:
            raise ValueError(f"role must be one of {', '.join(roles.ROLES)} or none")
        return v

    @field_validator("slack_user_id")
    @classmethod
    def _slack(cls, v):
        v = (v or "").strip().upper() or None
        if v and not SLACK_ID.match(v):
            raise ValueError("Slack user ids look like U0123ABCD")
        return v


class GroupIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    ticket_categories: list[str] = Field(default_factory=list)
    members: list[int] = Field(default_factory=list, max_length=200)

    @field_validator("ticket_categories")
    @classmethod
    def _cats(cls, v):
        bad = set(v) - set(CATEGORIES)
        if bad:
            raise ValueError(f"unknown categories: {', '.join(sorted(bad))}")
        return sorted(set(v))


class SettingsIn(BaseModel):
    slack_default_role: str | None

    @field_validator("slack_default_role")
    @classmethod
    def _role(cls, v):
        return PersonIn._role(v)


PERSON_SQL = """
select p.*,
  coalesce((select array_agg(gm.group_id order by gm.group_id) from group_members gm where gm.person_id = p.id), '{}') as groups,
  coalesce((select json_agg(json_build_object('title', t.title, 'seat', t.seat, 'how', t.how, 'start_date', t.start_date,
              'end_date', t.end_date, 'ended_on', t.ended_on, 'end_reason', t.end_reason, 'notes', t.notes)
              order by t.start_date desc) from board_terms t where t.person_id = p.id), '[]') as terms
from people_access p
{where} order by p.active desc, p.on_board desc, (p.effective_role is null), p.name nulls last, p.email"""


def _person(conn, pid: int) -> dict:
    row = conn.execute(PERSON_SQL.replace("{where}", "where p.id = %s"), (pid,)).fetchone()
    if not row:
        raise HTTPException(404, "Person not found")
    return row


def _admins_left(conn, excluding: int) -> int:
    return conn.execute("select count(*) as n from people where role = 'admin' and active and id <> %s",
                        (excluding,)).fetchone()["n"]


def _set_groups(conn, pid: int, groups: list[int]) -> None:
    conn.execute("delete from group_members where person_id = %s", (pid,))
    for g in set(groups):
        conn.execute("insert into group_members (group_id, person_id) select id, %s from groups where id = %s", (pid, g))


def _slack_lookup(email: str) -> tuple[str, str] | None:
    """Find a Slack user by email (users:read.email). Returns (id, display name) or None."""
    if not settings.slack_bot_token:
        return None
    try:
        from ..slack import bolt
        u = bolt().client.users_lookupByEmail(email=email)["user"]
        return u["id"], u.get("profile", {}).get("real_name") or u.get("name")
    except Exception:
        return None


def _write_person(conn, body: PersonIn, user: User, pid: int | None) -> int:
    fields = body.model_dump(exclude={"groups", "terms"})
    fields["phone"] = (fields["phone"] or "").strip() or None
    fields["responsibilities"] = (fields["responsibilities"] or "").strip() or None
    fields["slack_name"] = None
    if not fields["slack_user_id"]:
        if found := _slack_lookup(body.email):
            fields["slack_user_id"], fields["slack_name"] = found
    elif pid is not None:
        prev = conn.execute("select slack_user_id, slack_name from people where id = %s", (pid,)).fetchone()
        if prev and prev["slack_user_id"] == fields["slack_user_id"]:
            fields["slack_name"] = prev["slack_name"]
    taken = conn.execute("select id, email from people where (email = %s or slack_user_id = %s) and id <> %s",
                         (fields["email"], fields["slack_user_id"], pid or 0)).fetchone()
    if taken:
        raise HTTPException(409, f"{taken['email']} already has that email or Slack account")
    if pid is None:
        pid = conn.execute(
            """insert into people (email, name, role, active, slack_user_id, slack_name, notes, phone, responsibilities,
                                   created_by, updated_by)
               values (%(email)s, %(name)s, %(role)s, %(active)s, %(slack_user_id)s, %(slack_name)s, %(notes)s, %(phone)s,
                       %(responsibilities)s, %(by)s, %(by)s)
               returning id""", {**fields, "by": user.email}).fetchone()["id"]
    else:
        before = _person(conn, pid)
        if before["role"] == "admin" and before["active"] and (fields["role"] != "admin" or not fields["active"]) \
                and _admins_left(conn, pid) == 0:
            raise HTTPException(409, "That would leave Cypress with no admin. Make someone else an admin first.")
        conn.execute(
            """update people set email = %(email)s, name = %(name)s, role = %(role)s, active = %(active)s,
                 slack_user_id = %(slack_user_id)s, slack_name = %(slack_name)s, notes = %(notes)s,
                 phone = %(phone)s, responsibilities = %(responsibilities)s,
                 firebase_uid = case when email = %(email)s then firebase_uid end,
                 updated_by = %(by)s, updated_at = now() where id = %(id)s""",
            {**fields, "by": user.email, "id": pid})
    _set_groups(conn, pid, body.groups)
    conn.execute("delete from board_terms where person_id = %s", (pid,))
    for t in body.terms:
        conn.execute("""insert into board_terms (person_id, title, seat, how, start_date, end_date, ended_on, end_reason, notes)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (pid, t.title.strip(), t.seat, t.how, t.start_date, t.end_date, t.ended_on, t.end_reason, t.notes))
    if pid is not None and _would_orphan_admins(conn):
        raise HTTPException(409, "That would leave Cypress with no admin. Make someone else an admin first.")
    return pid


def _would_orphan_admins(conn) -> bool:
    return conn.execute("select count(*) as n from people_access where effective_role = 'admin'").fetchone()["n"] == 0


@router.get("/people")
def list_people(_: User = Depends(require("admin"))):
    with tx() as conn:
        rows = conn.execute(PERSON_SQL.replace("{where}", "")).fetchall()
        groups = conn.execute("""select g.*, coalesce(array_agg(gm.person_id) filter (where gm.person_id is not null), '{}')
                                 as members from groups g left join group_members gm on gm.group_id = g.id
                                 group by g.id order by g.name""").fetchall()
    return {"people": rows, "groups": groups, "roles": list(roles.ROLES), "categories": list(CATEGORIES),
            "slack_default_role": people.slack_default_role(), "slack_connected": bool(settings.slack_bot_token)}


@router.post("/people", status_code=201)
def add_person(body: PersonIn, user: User = Depends(require("admin"))):
    with tx() as conn:
        if conn.execute("select 1 from people where email = %s", (body.email,)).fetchone():
            raise HTTPException(409, f"{body.email} is already listed")
        pid = _write_person(conn, body, user, None)
        row = _person(conn, pid)
        audit(conn, user, "create", "person", pid, {"email": row["email"], "role": row["role"], "groups": row["groups"],
                                                    "terms": row["terms"]})
    people.clear()
    return row


@router.put("/people/{pid}")
def update_person(pid: int, body: PersonIn, user: User = Depends(require("admin"))):
    with tx() as conn:
        _person(conn, pid)
        _write_person(conn, body, user, pid)
        row = _person(conn, pid)
        audit(conn, user, "update", "person", pid, {"email": row["email"], "role": row["role"], "active": row["active"],
                                                    "effective_role": row["effective_role"], "slack_user_id": row["slack_user_id"],
                                                    "groups": row["groups"], "terms": row["terms"],
                                                    "responsibilities": row["responsibilities"]})
    people.clear()
    return row


@router.delete("/people/{pid}", status_code=204)
def remove_person(pid: int, user: User = Depends(require("admin"))):
    with tx() as conn:
        p = _person(conn, pid)
        if p["role"] == "admin" and p["active"] and _admins_left(conn, pid) == 0:
            raise HTTPException(409, "That would leave Cypress with no admin. Make someone else an admin first.")
        conn.execute("delete from people where id = %s", (pid,))
        audit(conn, user, "delete", "person", pid, {"email": p["email"], "role": p["role"]})
    people.clear()


@router.post("/people/slack-sync")
async def slack_sync(user: User = Depends(require("admin"))):
    """Link listed people to their Slack accounts by email; list workspace members not yet listed."""
    if not settings.slack_bot_token:
        raise HTTPException(409, "Slack isn't connected")

    def members() -> list[dict]:
        from ..slack import bolt
        out, cursor = [], None
        while True:
            r = bolt().client.users_list(limit=200, cursor=cursor)
            out += r["members"]
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return out

    try:
        found = await run_in_threadpool(members)
    except Exception as e:
        log.exception("users.list failed")
        raise HTTPException(502, f"Slack didn't answer: {e}") from None
    humans = [m for m in found if not m.get("is_bot") and not m.get("deleted") and m.get("id") != "USLACKBOT"]
    linked, unlisted = [], []
    with tx() as conn:
        for m in humans:
            email = (m.get("profile", {}).get("email") or "").lower()
            name = m.get("profile", {}).get("real_name") or m.get("name")
            guest = bool(m.get("is_restricted") or m.get("is_ultra_restricted"))
            row = conn.execute("select id, email, slack_user_id from people where slack_user_id = %s or (email = %s and %s <> '')",
                               (m["id"], email, email)).fetchone()
            if row:
                if row["slack_user_id"] != m["id"]:
                    if row["slack_user_id"]:
                        continue  # linked to someone else on purpose; leave it
                    linked.append(row["email"])
                conn.execute("update people set slack_user_id = %s, slack_name = %s where id = %s", (m["id"], name, row["id"]))
            else:
                unlisted.append({"slack_user_id": m["id"], "name": name, "email": email or None, "guest": guest})
        audit(conn, user, "slack_sync", "person", None, {"linked": linked, "unlisted": len(unlisted)})
    people.clear()
    return {"linked": linked, "unlisted": sorted(unlisted, key=lambda u: (u["guest"], (u["name"] or "").lower())),
            "workspace_members": len(humans)}


@router.put("/people-settings")
def put_settings(body: SettingsIn, user: User = Depends(require("admin"))):
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('slack_default_role', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                        updated_at = now()""", (Jsonb(body.slack_default_role or "none"), user.email))
        audit(conn, user, "update", "org_settings", "slack_default_role", {"value": body.slack_default_role})
    org.clear()
    people.clear()
    return {"slack_default_role": people.slack_default_role()}


def _write_group(conn, body: GroupIn, gid: int | None) -> int:
    clash = conn.execute("select id from groups where lower(name) = lower(%s) and id <> %s", (body.name, gid or 0)).fetchone()
    if clash:
        raise HTTPException(409, f"There's already a group called {body.name}")
    if gid is None:
        gid = conn.execute("insert into groups (name, description, ticket_categories) values (%s, %s, %s) returning id",
                           (body.name.strip(), body.description, body.ticket_categories)).fetchone()["id"]
    else:
        n = conn.execute("""update groups set name = %s, description = %s, ticket_categories = %s, updated_at = now()
                            where id = %s""", (body.name.strip(), body.description, body.ticket_categories, gid)).rowcount
        if not n:
            raise HTTPException(404, "Group not found")
    conn.execute("delete from group_members where group_id = %s", (gid,))
    for pid in set(body.members):
        conn.execute("insert into group_members (group_id, person_id) select %s, id from people where id = %s", (gid, pid))
    return gid


@router.post("/groups", status_code=201)
def add_group(body: GroupIn, user: User = Depends(require("admin"))):
    with tx() as conn:
        gid = _write_group(conn, body, None)
        audit(conn, user, "create", "group", gid, body.model_dump())
    return {"id": gid}


@router.put("/groups/{gid}")
def update_group(gid: int, body: GroupIn, user: User = Depends(require("admin"))):
    with tx() as conn:
        _write_group(conn, body, gid)
        audit(conn, user, "update", "group", gid, body.model_dump())
    return {"id": gid}


@router.delete("/groups/{gid}", status_code=204)
def remove_group(gid: int, user: User = Depends(require("admin"))):
    with tx() as conn:
        g = conn.execute("delete from groups where id = %s returning name", (gid,)).fetchone()
        if not g:
            raise HTTPException(404, "Group not found")
        audit(conn, user, "delete", "group", gid, {"name": g["name"]})


@router.get("/assignees")
def assignees(_: User = Depends(require("staff"))):
    """Names for the ticket assignee picker: active people with a staff-or-above role, and groups."""
    with tx() as conn:
        ppl = conn.execute("""select coalesce(name, email) as label from people_access
                              where effective_role in ('admin', 'board', 'staff', 'security') order by 1""").fetchall()
        grp = conn.execute("select name as label from groups order by 1").fetchall()
    return {"people": [r["label"] for r in ppl], "groups": [r["label"] for r in grp]}


# ---------------- Slack channels: who sees an answer ----------------

class ChannelIn(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    max_role: Literal["public", "member", "staff", "security", "board"] | None = None
    note: str | None = Field(default=None, max_length=300)


@router.get("/slack-channels")
async def slack_channels(_: User = Depends(require("admin"))):
    """Channels Cypress is in, the access level it answers at in each (what every member may see),
    and members who count as public because they aren't listed in Users & roles."""
    if not settings.slack_bot_token:
        raise HTTPException(409, "Slack isn't connected")
    from .. import slack

    def scan() -> list[dict]:
        client = slack.bolt().client
        chans, cursor = [], None
        while True:
            r = client.users_conversations(types="public_channel,private_channel", limit=200, cursor=cursor,
                                           exclude_archived=True)
            chans += r["channels"]
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        with tx() as conn:
            saved = {r["channel_id"]: r for r in conn.execute("select * from slack_channels")}
        me = slack.bot_user_id()
        out = []
        for c in chans:
            slack._members.pop(c["id"], None)          # fresh member list for the console
            members = slack.channel_members(c["id"])
            rows = []
            for m in members or []:
                if m == me:
                    continue
                role = slack.member_role(m)
                prof = slack._profiles.get(m, (0, {}))[1]
                rows.append({"slack_user_id": m, "name": (prof.get("profile") or {}).get("real_name") or prof.get("name"),
                             "role": role, "guest": bool(prof.get("is_restricted") or prof.get("is_ultra_restricted"))})
            auto = roles.common_role([r["role"] for r in rows]) if members is not None else None
            s = saved.get(c["id"])
            ceiling = s["max_role"] if s else None
            effective = roles.common_role([auto, None if ceiling == "public" else ceiling]) if ceiling else auto
            out.append({"channel_id": c["id"], "name": c.get("name"), "private": c.get("is_private", False),
                        "members_known": members is not None, "members": rows, "auto_role": auto,
                        "max_role": ceiling, "effective_role": effective, "note": s["note"] if s else None})
        return sorted(out, key=lambda c: c["name"] or "")

    try:
        return {"channels": await run_in_threadpool(scan)}
    except Exception as e:
        log.exception("listing Slack channels failed")
        raise HTTPException(502, f"Slack didn't answer: {e}") from None


@router.put("/slack-channels/{channel_id}")
def put_slack_channel(channel_id: str, body: ChannelIn, user: User = Depends(require("admin"))):
    if not re.fullmatch(r"[CG][A-Z0-9]{6,20}", channel_id):
        raise HTTPException(422, "Not a Slack channel id")
    with tx() as conn:
        conn.execute("""insert into slack_channels (channel_id, name, max_role, note, updated_by) values (%s, %s, %s, %s, %s)
                        on conflict (channel_id) do update set name = excluded.name, max_role = excluded.max_role,
                          note = excluded.note, updated_by = excluded.updated_by, updated_at = now()""",
                     (channel_id, body.name, body.max_role, body.note, user.email))
        audit(conn, user, "update", "slack_channel", channel_id, body.model_dump())
    return {"ok": True}
