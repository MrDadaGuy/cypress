"""Who has which role: the people table, looked up by Firebase sign-in or Slack user.

Roles live in Postgres (not in Firebase token claims) so a change applies everywhere within a
minute, including removing someone's access. Lookups are cached briefly per instance.
The view people_access applies board terms: an active term means board; when it ends, so does a
'board' role (see migrations/011_board_terms.sql).
"""

import logging
import time

from . import org, roles
from .db import tx

log = logging.getLogger(__name__)

TTL = 60
_web: dict[str, tuple[float, str | None]] = {}
_slack: dict[str, tuple[float, str | None]] = {}


def clear() -> None:
    _web.clear()
    _slack.clear()


def _fresh(cache: dict, key: str):
    hit = cache.get(key)
    return hit if hit and time.time() - hit[0] < TTL else None


def web_role(uid: str, email: str, email_verified: bool) -> str | None:
    """Role for a signed-in web user: people (board/staff/security/admin) first, then the member list.
    Unverified emails never get a role."""
    if hit := _fresh(_web, uid):
        return hit[1]
    role = None
    email = (email or "").lower()
    with tx() as conn:
        row = conn.execute("select id, effective_role from people_access where firebase_uid = %s", (uid,)).fetchone()
        if not row and email and email_verified:
            row = conn.execute("select id, effective_role from people_access where email = %s", (email,)).fetchone()
        if row:
            conn.execute("update people set firebase_uid = coalesce(firebase_uid, %s), last_seen_web = now() where id = %s",
                         (uid, row["id"]))
            role = row["effective_role"]
    if role is None:
        # Not board/staff/security: a resident whose own verified address is on the member list.
        from . import members
        role = members.member_role(uid, email, email_verified)
    _web[uid] = (time.time(), role)
    return role


def slack_default_role() -> str | None:
    r = org.get().get("slack_default_role")
    return r if r in roles.ROLES else None


def slack_role(user_id: str, profile_email: str | None, use_default: bool = True) -> str | None:
    """Role for a full Slack workspace member (guests and bots are handled by the caller).
    Unlisted members get the workspace default role, unless use_default=False (then public): channel
    audiences never trust the default, so an unlisted guard in a channel can't unlock board content."""
    if hit := _fresh(_slack, user_id):
        found, role = hit[1]
        return role if found or not use_default else slack_default_role()
    email = (profile_email or "").lower() or None
    with tx() as conn:
        row = conn.execute("select id, effective_role, slack_user_id from people_access where slack_user_id = %s",
                           (user_id,)).fetchone()
        if not row and email:
            row = conn.execute("select id, effective_role, slack_user_id from people_access where email = %s",
                               (email,)).fetchone()
            if row and row["slack_user_id"] and row["slack_user_id"] != user_id:
                log.warning("email %s is linked to another Slack user; not using it for %s", email, user_id)
                row = None
        if row:
            conn.execute("""update people set slack_user_id = coalesce(slack_user_id, %s), last_seen_slack = now()
                            where id = %s""", (user_id, row["id"]))
            role = row["effective_role"]
    _slack[user_id] = (time.time(), (row is not None, role if row else None))
    if row is None and use_default:
        return slack_default_role()
    return role if row else None


def mentions_for_category(category: str) -> list[str]:
    """Slack user ids to @-mention for a new ticket in this category (active, linked group members)."""
    with tx() as conn:
        rows = conn.execute(
            """select distinct p.slack_user_id from groups g
               join group_members gm on gm.group_id = g.id join people p on p.id = gm.person_id
               join people_access pa on pa.id = p.id
               where %s = any(g.ticket_categories) and pa.effective_role is not null and p.slack_user_id is not null
               order by 1""", (category,)).fetchall()
    return [r["slack_user_id"] for r in rows]


def board_roster() -> list[dict]:
    """People with an active board term, officers first."""
    order = "case lower(board_title) when 'president' then 0 when 'vice president' then 1 when 'secretary' then 2 " \
            "when 'treasurer' then 3 else 4 end"
    with tx() as conn:
        return conn.execute(f"""select id, coalesce(name, email) as name, board_title, term_end from people_access
                               where on_board and active order by {order}, name""").fetchall()


def routing_candidates() -> list[dict]:
    """Active people who can work tickets and have described their areas of responsibility."""
    with tx() as conn:
        return conn.execute("""select id, coalesce(name, email) as name, board_title, effective_role, responsibilities,
                                      slack_user_id from people_access
                               where effective_role in ('admin', 'board', 'staff', 'security')
                                 and coalesce(trim(responsibilities), '') <> '' order by id""").fetchall()


def terms_report(today=None) -> dict:
    """Terms ending within 60 days, and people whose term ended but who still hold admin."""
    with tx() as conn:
        ending = conn.execute("""select coalesce(p.name, p.email) as name, t.title, t.end_date from board_terms t
                                 join people p on p.id = t.person_id
                                 where p.active and t.ended_on is null
                                   and t.end_date between (now() at time zone 'America/Chicago')::date
                                                      and (now() at time zone 'America/Chicago')::date + 60
                                 order by t.end_date""").fetchall()
        lapsed_admins = conn.execute("""select coalesce(name, email) as name from people_access pa
                                        where effective_role = 'admin' and not on_board
                                          and exists (select 1 from board_terms t where t.person_id = pa.id)""").fetchall()
    return {"ending": ending, "lapsed_admins": [r["name"] for r in lapsed_admins]}
