"""Retention and legal hold.

Nightly (inside the 3:30 AM scheduler call) Cypress removes:
  - photos and videos never attached to a report (abandoned chats, unfinished video uploads),
  - attachments on tickets that have been resolved or closed for a while (the ticket and its history stay;
    the ticket shows the attachments were removed and when),
  - old entries in the member lookup log.
Open tickets are never touched. The periods are admin settings (org_settings 'retention'); 0 days = keep.

A legal hold (on one ticket, or on everything) stops all of it. Held objects also get a temporary hold in
the bucket, so the bucket's own 2-year safety-net rule can't delete them either.
"""

import logging

from .audit import audit
from .auth import User
from .config import settings
from .db import tx

log = logging.getLogger(__name__)

DEFAULTS = {"unattached_hours": 24, "closed_ticket_days": 180, "lookup_log_days": 365,
            "hold_all": False, "hold_all_reason": None}
CLOSED = ("resolved", "closed")


def get() -> dict:
    from . import org
    return {**DEFAULTS, **(org.get().get("retention") or {})}


# ---------------- what's due ----------------

def due_unattached(s: dict) -> list[dict]:
    """Uploads never attached to a report (including unfinished video slots)."""
    if s["hold_all"]:
        return []
    with tx() as conn:
        return conn.execute(
            """select id::text, path, kind, bytes from uploads
               where ticket_id is null and created_at < now() - make_interval(hours => %s)""",
            (int(s["unattached_hours"]),)).fetchall()


def due_closed(s: dict) -> list[dict]:
    """Attachments on tickets closed longer than the setting, unless the ticket is on hold."""
    days = int(s["closed_ticket_days"] or 0)
    if s["hold_all"] or days <= 0:
        return []
    with tx() as conn:
        return conn.execute(
            """select u.id::text, u.path, u.kind, u.bytes, u.ticket_id from uploads u join tickets t on t.id = u.ticket_id
               where u.removed_at is null and t.status = any(%s)
                 and t.updated_at < now() - make_interval(days => %s)
                 and not exists (select 1 from legal_holds h where h.ticket_id = u.ticket_id)""",
            (list(CLOSED), days)).fetchall()


# ---------------- bucket ----------------

def _bucket():
    from .uploads import _bucket
    return _bucket()


def _delete_object(path: str) -> bool:
    """True when the object is gone (or was never there)."""
    if not settings.uploads_bucket:
        from .uploads import LOCAL_DIR
        (LOCAL_DIR / path).unlink(missing_ok=True)
        return True
    from google.api_core.exceptions import NotFound
    try:
        _bucket().blob(path).delete()
        return True
    except NotFound:
        return True
    except Exception:  # e.g. a temporary hold we didn't know about
        log.exception("retention: couldn't delete %s", path)
        return False


def _set_object_hold(path: str, on: bool) -> bool:
    if not settings.uploads_bucket:
        return True
    from google.api_core.exceptions import NotFound
    try:
        blob = _bucket().blob(path)
        blob.temporary_hold = on
        blob.patch()
        return True
    except NotFound:
        return True
    except Exception:
        log.exception("retention: couldn't %s hold on %s", "set" if on else "clear", path)
        return False


def sync_holds(ticket_id: int | None = None) -> int:
    """Make the bucket's temporary holds match: held = everything on hold, or the ticket is on hold."""
    hold_all = bool(get()["hold_all"])
    q = """select u.id::text, u.path, u.held,
                  (%s or exists (select 1 from legal_holds h where h.ticket_id = u.ticket_id)) as want
           from uploads u where u.removed_at is null"""
    args: list = [hold_all]
    if ticket_id is not None:
        q += " and u.ticket_id = %s"
        args.append(ticket_id)
    with tx() as conn:
        rows = [r for r in conn.execute(q, args).fetchall() if r["held"] != r["want"]]
    changed = 0
    for r in rows:
        if _set_object_hold(r["path"], r["want"]):
            with tx() as conn:
                conn.execute("update uploads set held = %s where id = %s", (r["want"], r["id"]))
            changed += 1
    return changed


# ---------------- nightly run ----------------

def run() -> dict:
    s = get()
    holds = sync_holds()
    out = {"photos": 0, "videos": 0, "bytes": 0, "unattached": 0, "lookups": 0, "failed": 0, "holds_synced": holds,
           "hold_all": bool(s["hold_all"])}

    for u in due_unattached(s):
        if _delete_object(u["path"]):
            with tx() as conn:
                conn.execute("delete from uploads where id = %s and ticket_id is null", (u["id"],))
            out["unattached"] += 1
            out["bytes"] += u["bytes"] or 0
        else:
            out["failed"] += 1

    for u in due_closed(s):
        if _delete_object(u["path"]):
            with tx() as conn:
                conn.execute("update uploads set removed_at = now() where id = %s", (u["id"],))
            out["photos" if u["kind"] == "photo" else "videos"] += 1
            out["bytes"] += u["bytes"] or 0
        else:
            out["failed"] += 1

    days = int(s["lookup_log_days"] or 0)
    if days > 0 and not s["hold_all"]:
        with tx() as conn:
            out["lookups"] = conn.execute("delete from member_lookups where at < now() - make_interval(days => %s)",
                                          (days,)).rowcount

    from psycopg.types.json import Jsonb
    with tx() as conn:
        conn.execute("insert into retention_runs (summary) values (%s)", (Jsonb(out),))
        conn.execute("delete from retention_runs where at < now() - interval '1 year'")
    log.info("retention cleanup", extra={"json_fields": {"retention": out}})
    return out


def overnight() -> dict | None:
    """What the last day's cleanup removed, for the morning digest (counts only)."""
    with tx() as conn:
        rows = conn.execute("select summary from retention_runs where at > now() - interval '1 day'").fetchall()
    if not rows:
        return None
    keys = ("photos", "videos", "unattached", "lookups", "failed")
    total = {k: sum(int(r["summary"].get(k, 0)) for r in rows) for k in keys}
    total["hold_all"] = any(r["summary"].get("hold_all") for r in rows)
    return total


# ---------------- legal holds ----------------

class HoldError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def hold_for(ticket_id: int) -> dict | None:
    with tx() as conn:
        return conn.execute("select set_at, set_by, reason from legal_holds where ticket_id = %s", (ticket_id,)).fetchone()


def set_hold(ticket_id: int, on: bool, reason: str | None, user: User) -> dict | None:
    with tx() as conn:
        if not conn.execute("select 1 from tickets where id = %s", (ticket_id,)).fetchone():
            raise HoldError(404, "No such ticket")
        if on:
            reason = (reason or "").strip()
            if len(reason) < 3:
                raise HoldError(422, "Say why the ticket is on hold (for example, the matter or who asked).")
            conn.execute("""insert into legal_holds (ticket_id, set_by, reason) values (%s, %s, %s)
                            on conflict (ticket_id) do update set reason = excluded.reason""",
                         (ticket_id, user.email, reason[:500]))
            audit(conn, user, "legal_hold", "ticket", ticket_id, {"reason": reason[:500]})
        else:
            prev = conn.execute("delete from legal_holds where ticket_id = %s returning reason", (ticket_id,)).fetchone()
            if prev:
                audit(conn, user, "release_hold", "ticket", ticket_id, {"reason": prev["reason"]})
    sync_holds(ticket_id)
    return hold_for(ticket_id)


def status() -> dict:
    """For the admin panel: what's stored, what's on hold, and the last cleanup."""
    from datetime import UTC, datetime

    from .recrawl import next_run
    with tx() as conn:
        stored = conn.execute(
            """select count(*) filter (where kind = 'photo')::int as photos,
                      count(*) filter (where kind = 'video')::int as videos,
                      coalesce(sum(bytes), 0)::bigint as bytes,
                      count(*) filter (where held)::int as held
               from uploads where removed_at is null and status = 'ready'""").fetchone()
        tickets_held = conn.execute(
            """select h.ticket_id, h.set_at, h.set_by, h.reason, t.title from legal_holds h
               join tickets t on t.id = h.ticket_id order by h.set_at desc""").fetchall()
        last = conn.execute("select at, summary from retention_runs order by at desc limit 1").fetchone()
    return {"stored": stored, "holds": tickets_held, "last_run": last,
            "next_run": next_run(datetime.now(UTC)).isoformat()}
