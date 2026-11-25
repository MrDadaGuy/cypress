"""Members: matching a signed-in resident to the membership list, and actions that need a member.

Privacy rules (see roles.py): member records are never retrievable by the agent and never shown to
anyone but admins in the console. A resident who signs in learns only whether *their own* verified
address is on the list (it grants the "member" role). One or two addresses per household.
"""

import csv
import io
import logging
import re

from psycopg.types.json import Jsonb

from . import org
from .db import tx

log = logging.getLogger(__name__)

DEFAULTS = {"require_for_reports": True, "nudge_after_turns": 3, "pending_hours": 24}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def auth_settings() -> dict:
    return {**DEFAULTS, **(org.get().get("member_auth") or {})}


def member_role(uid: str, email: str, email_verified: bool) -> str | None:
    """'member' if this signed-in user's verified address belongs to an active household."""
    email = (email or "").lower()
    with tx() as conn:
        row = conn.execute(
            """select m.id from member_emails m join households h on h.id = m.household_id
               where m.firebase_uid = %s and h.status = 'active'""", (uid,)).fetchone()
        if not row and email and email_verified:
            row = conn.execute(
                """select m.id from member_emails m join households h on h.id = m.household_id
                   where m.email = %s and h.status = 'active'""", (email,)).fetchone()
        if not row:
            return None
        conn.execute("""update member_emails set firebase_uid = coalesce(firebase_uid, %s), last_signed_in = now()
                        where id = %s""", (uid, row["id"]))
    return "member"


# ---------------- actions that wait for sign-in ----------------

class PendingError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


def create_pending(kind: str, payload: dict, attachments: list[str], channel: str) -> str:
    hours = int(auth_settings().get("pending_hours") or 24)
    with tx() as conn:
        conn.execute("delete from pending_actions where expires_at < now() - interval '7 days'")
        return str(conn.execute(
            """insert into pending_actions (kind, payload, attachments, channel, expires_at)
               values (%s, %s, %s, %s, now() + make_interval(hours => %s)) returning id""",
            (kind, Jsonb(payload), attachments, channel, hours)).fetchone()["id"])


def pending_summary(pid: str) -> dict | None:
    """What the sign-in card shows: the kind and the title, nothing personal."""
    with tx() as conn:
        r = conn.execute("""select kind, payload->>'title' as title, completed_at is not null as done,
                                   expires_at < now() as expired
                            from pending_actions where id = %s""", (pid,)).fetchone()
    return dict(r) if r else None


def complete_pending(pid: str, *, role: str | None, email: str, uid: str) -> dict:
    """Run a waiting action for a signed-in member. Each action runs once."""
    from . import notify, tickets, uploads
    if role is None:
        raise PendingError(403, "That email address isn't on the LHCA member list.")
    with tx() as conn:
        p = conn.execute("select * from pending_actions where id = %s for update", (pid,)).fetchone()
        if not p:
            raise PendingError(404, "That request wasn't found.")
        if p["completed_at"]:
            if p["completed_by"] == email:
                return p["result"]           # e.g. the link was opened twice
            raise PendingError(409, "That request was already completed.")
        if p["expires_at"] < conn.execute("select now() as n").fetchone()["n"]:
            raise PendingError(410, "That request expired. Please describe the problem again.")
        a = p["payload"]
        t = tickets.create(role=role, actor=email, channel=p["channel"], title=a["title"], description=a["description"],
                           category=a.get("category") or "other", priority=a.get("urgency") or "normal",
                           location=a.get("location"), reporter_uid=uid, reporter_contact=a.get("contact") or email)
        result = {"kind": p["kind"], "ticket_number": t["id"], "title": t["title"], "status": t["status"]}
        conn.execute("update pending_actions set completed_at = now(), completed_by = %s, result = %s where id = %s",
                     (email, Jsonb(result), pid))
    photos = uploads.attach(list(p["attachments"] or []), t["id"])
    result["photos_attached"] = photos
    result["board_notified"] = notify.new_ticket(t, photos=photos)
    return result


# ---------------- import ----------------

ALIASES = {
    "household": ("household", "household name", "family", "last name", "lastname", "name", "owner", "owners", "member"),
    "address": ("address", "street address", "property address", "street"),
    "account_ref": ("account", "account #", "account number", "member id", "id", "lot", "membership #", "membership number"),
    "email1": ("email", "email 1", "email1", "primary email", "e-mail", "email address"),
    "email2": ("email 2", "email2", "secondary email", "alternate email", "spouse email", "other email"),
    "name1": ("first name", "first", "contact", "contact name", "name 1"),
    "name2": ("name 2", "second name", "spouse", "spouse name", "co-owner"),
    "status": ("status", "active", "membership status", "paid", "dues status"),
    "phone1": ("phone", "phone 1", "phone1", "primary phone", "cell", "cell phone", "mobile", "home phone", "telephone"),
    "phone2": ("phone 2", "phone2", "secondary phone", "alternate phone", "work phone", "other phone", "spouse phone"),
}
FIELDS = tuple(ALIASES)
INACTIVE = {"inactive", "lapsed", "no", "false", "0", "unpaid", "expired", "former", "n"}


def guess_mapping(headers: list[str]) -> dict[str, str | None]:
    norm = {h: re.sub(r"\s+", " ", h.strip().lower().replace("_", " ")) for h in headers}
    used, out = set(), {}
    for field in FIELDS:
        out[field] = next((h for h in headers if h not in used and norm[h] in ALIASES[field]), None)
        if out[field]:
            used.add(out[field])
    return out


def read_csv(data: bytes) -> tuple[list[str], list[dict]]:
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    headers = list(rows[0].keys()) if rows else []
    return [h for h in headers if h], rows


def normalize(rows: list[dict], mapping: dict[str, str | None]) -> tuple[list[dict], list[str]]:
    """Rows -> households with up to two emails. Returns (households, problems)."""
    get = lambda r, f: (r.get(mapping.get(f) or "", "") or "").strip()
    out, problems, seen = [], [], {}
    for i, r in enumerate(rows, start=2):           # line 1 is the header
        name = get(r, "household")
        emails = []
        for f in ("email1", "email2"):
            for e in re.split(r"[;,\s]+", get(r, f).lower()):
                if not e:
                    continue
                if not EMAIL.match(e):
                    problems.append(f"Line {i}: '{e}' isn't an email address")
                elif e in seen:
                    problems.append(f"Line {i}: {e} is also on line {seen[e]}")
                elif e not in emails:
                    seen[e] = i
                    emails.append(e)
        if not name and not emails:
            continue
        if not name:
            name = get(r, "address") or emails[0]
        status = "inactive" if get(r, "status").lower() in INACTIVE else "active"
        phones = []
        for f in ("phone1", "phone2"):
            digits = re.sub(r"\D", "", get(r, f))
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) == 10 and digits not in phones:
                phones.append(digits)
            elif digits:
                problems.append(f"Line {i}: '{get(r, f)}' isn't a 10-digit phone number")
        out.append({"line": i, "name": name[:200], "address": get(r, "address")[:300] or None,
                    "account_ref": get(r, "account_ref")[:100] or None, "status": status, "phones": phones,
                    "emails": [{"email": e, "name": (get(r, "name1") if j == 0 else get(r, "name2"))[:120] or None}
                               for j, e in enumerate(emails[:2])]})
        if len(emails) > 2:
            problems.append(f"Line {i}: more than two emails; kept the first two")
    return out, problems


def commit_import(households: list[dict], *, replace: bool, source: str) -> dict:
    """Upsert households (matched by account ref, else by email) and their emails.
    replace=True marks households missing from this list inactive (their members lose access)."""
    stats = {"created": 0, "updated": 0, "emails": 0, "deactivated": 0}
    touched: set[int] = set()
    with tx() as conn:
        for h in households:
            hid = None
            if h["account_ref"]:
                r = conn.execute("select id from households where account_ref = %s", (h["account_ref"],)).fetchone()
                hid = r and r["id"]
            if not hid and h["emails"]:
                r = conn.execute("select household_id from member_emails where email = any(%s) limit 1",
                                 ([e["email"] for e in h["emails"]],)).fetchone()
                hid = r and r["household_id"]
            if hid:
                conn.execute("""update households set name = %s, address = coalesce(%s, address),
                                  account_ref = coalesce(%s, account_ref), status = %s, source = %s, updated_at = now(),
                                  phones = case when cardinality(%s::text[]) > 0 then %s::text[] else phones end
                                where id = %s""", (h["name"], h["address"], h["account_ref"], h["status"], source,
                                                   h.get("phones") or [], h.get("phones") or [], hid))
                stats["updated"] += 1
            else:
                hid = conn.execute("""insert into households (name, address, account_ref, status, source, phones)
                                      values (%s, %s, %s, %s, %s, %s) returning id""",
                                   (h["name"], h["address"], h["account_ref"], h["status"], source,
                                    h.get("phones") or [])).fetchone()["id"]
                stats["created"] += 1
            touched.add(hid)
            keep = [e["email"] for e in h["emails"]]
            conn.execute("delete from member_emails where household_id = %s and not (email = any(%s))", (hid, keep))
            for e in h["emails"]:
                conn.execute("""insert into member_emails (household_id, email, name) values (%s, %s, %s)
                                on conflict (email) do update set household_id = excluded.household_id,
                                  name = coalesce(excluded.name, member_emails.name)""", (hid, e["email"], e["name"]))
                stats["emails"] += 1
        if replace:
            stats["deactivated"] = conn.execute(
                """update households set status = 'inactive', updated_at = now()
                   where status = 'active' and not (id = any(%s))""", (list(touched),)).rowcount
    return stats
