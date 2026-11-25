"""Admin: the membership list (households and their 1-2 emails) and member sign-in settings.

Admin-only. Member data never leaves this API: it isn't retrievable by the agent, the audit log
records counts (not addresses), and uploaded files are parsed in memory and not stored.
"""

import json
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import members, org, people
from ..audit import audit
from ..auth import User, require
from ..db import tx

router = APIRouter(prefix="/api/admin", tags=["admin: members"])
admin = require("admin")
MAX_BYTES = 5_000_000


class EmailIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    name: str | None = Field(default=None, max_length=120)


class HouseholdIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    account_ref: str | None = Field(default=None, max_length=100)
    status: Literal["active", "inactive"] = "active"
    notes: str | None = Field(default=None, max_length=1000)
    emails: list[EmailIn] = Field(default_factory=list, max_length=2)


class MemberAuthIn(BaseModel):
    require_for_reports: bool
    nudge_after_turns: int = Field(ge=0, le=50)
    pending_hours: int = Field(ge=1, le=168)


@router.get("/members")
def list_members(q: str = "", status: Literal["all", "active", "inactive"] = "all",
                 limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), _: User = Depends(admin)):
    where, args = ["true"], {"q": f"%{q.strip().lower()}%", "status": status, "limit": limit, "offset": offset}
    if q.strip():
        where.append("""(lower(h.name) like %(q)s or lower(coalesce(h.address, '')) like %(q)s
                         or lower(coalesce(h.account_ref, '')) like %(q)s
                         or exists (select 1 from member_emails e where e.household_id = h.id and e.email like %(q)s))""")
    if status != "all":
        where.append("h.status = %(status)s")
    with tx() as conn:
        rows = conn.execute(f"""
            select h.*, coalesce(json_agg(json_build_object('email', e.email, 'name', e.name,
                     'last_signed_in', e.last_signed_in) order by e.id) filter (where e.id is not null), '[]') as emails,
                   count(*) over () as total
            from households h left join member_emails e on e.household_id = h.id
            where {' and '.join(where)} group by h.id order by lower(h.name), h.id
            limit %(limit)s offset %(offset)s""", args).fetchall()
        counts = conn.execute("""
            select count(*) filter (where status = 'active') as active, count(*) as households,
                   (select count(*) from member_emails) as emails,
                   (select count(*) from member_emails where last_signed_in is not null) as signed_in
            from households""").fetchone()
    hidden = {"total", "dues_balance", "dues_balance_at"}      # balances are stored, not shown or used (yet)
    return {"households": [{k: v for k, v in r.items() if k not in hidden} for r in rows],
            "total": rows[0]["total"] if rows else 0, "counts": counts, "settings": members.auth_settings()}


async def _read(file: UploadFile) -> bytes:
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "That file is over 5 MB")
    return data


@router.post("/members/import/preview")
async def preview(file: UploadFile = File(...), mapping: str | None = Form(None), _: User = Depends(admin)):
    """Read a CSV and show what an import would do. Nothing is saved."""
    headers, rows = members.read_csv(await _read(file))
    if not headers:
        raise HTTPException(422, "That file has no header row")
    m = json.loads(mapping) if mapping else members.guess_mapping(headers)
    if not (m.get("email1") or m.get("email2")):
        return {"headers": headers, "mapping": m, "rows": len(rows), "households": 0, "sample": [],
                "problems": ["Choose which column holds email addresses."]}
    hh, problems = members.normalize(rows, m)
    return {"headers": headers, "mapping": m, "rows": len(rows), "households": len(hh),
            "emails": sum(len(h["emails"]) for h in hh), "inactive": sum(1 for h in hh if h["status"] == "inactive"),
            "no_email": sum(1 for h in hh if not h["emails"]), "sample": hh[:5], "problems": problems[:50],
            "more_problems": max(0, len(problems) - 50)}


@router.post("/members/import")
async def do_import(file: UploadFile = File(...), mapping: str = Form(...), replace: bool = Form(False),
                    user: User = Depends(admin)):
    headers, rows = members.read_csv(await _read(file))
    hh, problems = members.normalize(rows, json.loads(mapping))
    if not hh:
        raise HTTPException(422, "Nothing to import")
    stats = members.commit_import(hh, replace=replace, source=f"{file.filename or 'upload'} by {user.email}")
    with tx() as conn:
        audit(conn, user, "import", "members", None, {**stats, "file": file.filename, "replace": replace,
                                                      "skipped_problems": len(problems)})
    people.clear()
    from .. import vehicles
    stats["vehicles_linked"] = vehicles.relink()
    return {**stats, "problems": len(problems)}


# ---------------- vehicles ----------------

@router.get("/vehicles")
def list_vehicles(q: str = "", limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                  unmatched: bool = False, _: User = Depends(admin)):
    from .. import vehicles
    where, args = ["true"], {"q": f"%{q.strip().lower()}%", "p": f"%{vehicles.norm_plate(q)}%", "n": limit, "o": offset}
    if q.strip():
        where.append("""(v.plate_norm like %(p)s or lower(coalesce(v.make, '') || ' ' || coalesce(v.model, '') || ' '
                         || coalesce(v.color, '')) like %(q)s or lower(coalesce(h.name, '')) like %(q)s
                         or lower(coalesce(h.address, v.address, '')) like %(q)s or lower(coalesce(v.tag, '')) like %(q)s)""")
    if unmatched:
        where.append("v.household_id is null")
    with tx() as conn:
        rows = conn.execute(f"""
            select v.id, v.plate, v.state, v.make, v.model, v.color, v.year, v.tag, v.notes, v.household_id,
                   coalesce(h.name, null) as household, coalesce(h.address, v.address) as address, h.status,
                   v.updated_at, count(*) over () as total
            from vehicles v left join households h on h.id = v.household_id
            where {' and '.join(where)} order by v.plate_norm limit %(n)s offset %(o)s""", args).fetchall()
        counts = conn.execute("""select count(*)::int as vehicles, count(*) filter (where household_id is null)::int as unmatched
                                 from vehicles""").fetchone()
    return {"vehicles": [{k: v for k, v in r.items() if k != "total"} for r in rows],
            "total": rows[0]["total"] if rows else 0, "counts": counts}


@router.post("/vehicles/import/preview")
async def preview_vehicles(file: UploadFile = File(...), mapping: str | None = Form(None), _: User = Depends(admin)):
    """Read a vehicle CSV and show what an import would do. Nothing is saved."""
    from .. import vehicles
    headers, rows = members.read_csv(await _read(file))
    if not headers:
        raise HTTPException(422, "That file has no header row")
    m = json.loads(mapping) if mapping else vehicles.guess_mapping(headers)
    if not m.get("plate"):
        return {"headers": headers, "mapping": m, "rows": len(rows), "vehicles": 0, "sample": [],
                "problems": ["Choose which column holds the license plate."]}
    vs, problems = vehicles.normalize(rows, m)
    return {"headers": headers, "mapping": m, "rows": len(rows), "vehicles": len(vs),
            "matched": sum(1 for v in vs if v["household_id"]), "unmatched": sum(1 for v in vs if not v["household_id"]),
            "sample": [{k: v[k] for k in ("line", "plate", "state", "make", "model", "color", "year", "household")} for v in vs[:5]],
            "problems": problems[:50], "more_problems": max(0, len(problems) - 50)}


@router.post("/vehicles/import")
async def import_vehicles(file: UploadFile = File(...), mapping: str = Form(...), replace: bool = Form(False),
                          user: User = Depends(admin)):
    from .. import vehicles
    headers, rows = members.read_csv(await _read(file))
    vs, problems = vehicles.normalize(rows, json.loads(mapping))
    if not vs:
        raise HTTPException(422, "Nothing to import")
    stats = vehicles.commit_import(vs, replace=replace, source=f"{file.filename or 'upload'} by {user.email}")
    with tx() as conn:
        audit(conn, user, "import", "vehicles", None, {**stats, "file": file.filename, "replace": replace,
                                                       "skipped_problems": len(problems)})
    return {**stats, "problems": len(problems)}


@router.delete("/vehicles/{vid}", status_code=204)
def delete_vehicle(vid: int, user: User = Depends(admin)):
    with tx() as conn:
        if not conn.execute("delete from vehicles where id = %s returning id", (vid,)).fetchone():
            raise HTTPException(404, "Vehicle not found")
        audit(conn, user, "delete", "vehicle", vid)


def _save_household(conn, hid: int | None, body: HouseholdIn) -> int:
    emails = [e.email.lower() for e in body.emails]
    if len(set(emails)) != len(emails):
        raise HTTPException(422, "The same email is listed twice")
    taken = conn.execute("select email from member_emails where email = any(%s) and household_id is distinct from %s",
                         (emails, hid)).fetchone()
    if taken:
        raise HTTPException(409, f"{taken['email']} belongs to another household")
    if hid is None:
        hid = conn.execute("""insert into households (name, address, account_ref, status, notes, source)
                              values (%s, %s, %s, %s, %s, 'console') returning id""",
                           (body.name, body.address, body.account_ref, body.status, body.notes)).fetchone()["id"]
    else:
        if not conn.execute("""update households set name = %s, address = %s, account_ref = %s, status = %s, notes = %s,
                                 updated_at = now() where id = %s returning id""",
                            (body.name, body.address, body.account_ref, body.status, body.notes, hid)).fetchone():
            raise HTTPException(404, "Household not found")
        conn.execute("delete from member_emails where household_id = %s and not (email = any(%s))", (hid, emails))
    for e in body.emails:
        conn.execute("""insert into member_emails (household_id, email, name) values (%s, %s, %s)
                        on conflict (email) do update set name = excluded.name""", (hid, e.email.lower(), e.name))
    return hid


@router.post("/households", status_code=201)
def add_household(body: HouseholdIn, user: User = Depends(admin)):
    with tx() as conn:
        hid = _save_household(conn, None, body)
        audit(conn, user, "create", "household", hid, {"emails": len(body.emails), "status": body.status})
    people.clear()
    return {"id": hid}


@router.put("/households/{hid}")
def save_household(hid: int, body: HouseholdIn, user: User = Depends(admin)):
    with tx() as conn:
        _save_household(conn, hid, body)
        audit(conn, user, "update", "household", hid, {"emails": len(body.emails), "status": body.status})
    people.clear()
    return {"id": hid}


@router.delete("/households/{hid}", status_code=204)
def delete_household(hid: int, user: User = Depends(admin)):
    with tx() as conn:
        if not conn.execute("delete from households where id = %s returning id", (hid,)).fetchone():
            raise HTTPException(404, "Household not found")
        audit(conn, user, "delete", "household", hid)
    people.clear()


@router.put("/member-settings")
def put_member_settings(body: MemberAuthIn, user: User = Depends(admin)):
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('member_auth', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                          updated_at = now()""", (Jsonb(body.model_dump()), user.email))
        audit(conn, user, "update", "org_settings", "member_auth", body.model_dump())
    org.clear()
    return members.auth_settings()


# ---------------- member lookup (board, staff and security) ----------------

class LookupIn(BaseModel):
    access: dict[Literal["security", "board", "admin"], Literal["details", "confirm", "off"]]
    per_hour: int = Field(ge=1, le=500)
    per_day: int = Field(ge=1, le=5000)


@router.get("/member-lookup")
def get_member_lookup(limit: int = 100, _: User = Depends(admin)):
    from .. import member_lookup
    return {"settings": member_lookup.settings(), "log": member_lookup.recent(min(max(limit, 1), 500))}


@router.put("/member-lookup")
def put_member_lookup(body: LookupIn, user: User = Depends(admin)):
    from .. import member_lookup
    value = body.model_dump()
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('member_lookup', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                          updated_at = now()""", (Jsonb(value), user.email))
        audit(conn, user, "update", "org_settings", "member_lookup", value)
    org.clear()
    return {"settings": member_lookup.settings(), "log": member_lookup.recent(100)}


# ---------------- incident case files ----------------

class CasesIn(BaseModel):
    full_roles: list[Literal["board", "admin", "security"]]
    vision: bool
    draft_letters: bool
    policy_reasoning: Literal["linear", "tot"] = "linear"


@router.get("/cases-settings")
def get_cases_settings(_: User = Depends(admin)):
    from .. import cases
    return cases.settings()


@router.put("/cases-settings")
def put_cases_settings(body: CasesIn, user: User = Depends(admin)):
    from .. import cases
    value = {**body.model_dump(), "full_roles": sorted(set(body.full_roles) | {"admin"})}
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('cases', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                          updated_at = now()""", (Jsonb(value), user.email))
        audit(conn, user, "update", "org_settings", "cases", value)
    org.clear()
    return cases.settings()


# ---------------- CAM's membership workbook: upload, scheduled sync, color legend, types ----------------

def _sync_view() -> dict:
    from .. import org, sheet_sync, workbook
    return {"settings": sheet_sync.settings(), "service_account": sheet_sync.service_account(), "runs": sheet_sync.recent(10),
            "colors": workbook.legend(), "types": org.get().get("member_types") or {}}


@router.get("/member-sync")
def get_member_sync(_: User = Depends(admin)):
    return _sync_view()


@router.post("/members/workbook/preview")
async def preview_workbook(file: UploadFile = File(...), _: User = Depends(admin)):
    """Read CAM's workbook (.xlsx) and show what an import would do, including what changed. Nothing is saved."""
    from .. import sheet_sync, workbook
    data = await _read_xlsx(file)
    try:
        return await run_in_threadpool(workbook.preview, data, sheet_sync.settings()["tab_kinds"])
    except Exception as e:
        raise HTTPException(422, f"That file couldn't be read as an Excel workbook ({type(e).__name__}).") from None


@router.post("/members/workbook/import")
async def import_workbook(file: UploadFile = File(...), replace: bool = Form(True), user: User = Depends(admin)):
    from .. import sheet_sync, workbook
    data = await _read_xlsx(file)
    s = sheet_sync.settings()
    r = await run_in_threadpool(lambda: workbook.run(data, source="upload", replace=replace,
                                                     min_fraction=float(s["min_fraction"]), overrides=s["tab_kinds"]))
    run = await run_in_threadpool(sheet_sync.record_upload, r["stats"], r["changes"], r["warnings"])
    with tx() as conn:
        audit(conn, user, "import", "workbook", None, {"file": file.filename, "replace": replace, **{
            k: v for k, v in r["stats"].items() if k != "vehicles_linked"}})
    return {**r, "run": run}


async def _read_xlsx(file: UploadFile) -> bytes:
    data = await file.read(20_000_001)
    if len(data) > 20_000_000:
        raise HTTPException(413, "That file is over 20 MB")
    if not data.startswith(b"PK"):
        raise HTTPException(422, "Upload the workbook as .xlsx (in Google Sheets: File → Download → Microsoft Excel)")
    return data


class SyncCheckIn(BaseModel):
    file: str = Field(min_length=10, max_length=400)


class SyncIn(BaseModel):
    file: str | None = Field(None, max_length=400)
    contacts_file: str | None = Field(None, max_length=400)      # "" removes it
    enabled: bool
    replace: bool = True
    min_fraction: float = Field(0.8, ge=0.1, le=1.0)
    tab_kinds: dict[str, Literal["households", "former", "golf_carts", "vendors", "ignore"]] = Field(default_factory=dict)


@router.post("/member-sync/check")
def check_member_sync(body: SyncCheckIn, _: User = Depends(admin)):
    """Open the file in Drive and preview an import. Nothing is saved."""
    from .. import sheet_sync
    try:
        return sheet_sync.check(body.file)
    except sheet_sync.SheetError as e:
        raise HTTPException(422, str(e)) from None


@router.put("/member-sync")
def put_member_sync(body: SyncIn, user: User = Depends(admin)):
    from .. import sheet_sync
    cur = sheet_sync.settings()
    value = {**cur, "enabled": body.enabled, "replace": body.replace, "min_fraction": body.min_fraction, "tab_kinds": body.tab_kinds}
    if body.file:
        try:
            fid = sheet_sync.file_id(body.file)
            _, meta = sheet_sync.fetch(fid)
        except sheet_sync.SheetError as e:
            raise HTTPException(422, str(e)) from None
        value.update(file_id=fid, file_url=body.file, file_name=meta.get("name"))
    if body.contacts_file == "":
        value.update(contacts_file_id=None, contacts_file_url=None, contacts_file_name=None)
    elif body.contacts_file:
        try:
            cid = sheet_sync.file_id(body.contacts_file)
            _, meta = sheet_sync.fetch(cid)
        except sheet_sync.SheetError as e:
            raise HTTPException(422, f"Contacts file: {e}") from None
        value.update(contacts_file_id=cid, contacts_file_url=body.contacts_file, contacts_file_name=meta.get("name"))
    if value["enabled"] and not value.get("file_id"):
        raise HTTPException(422, "Add the file's link before turning the sync on")
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('member_sync', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                          updated_at = now()""", (Jsonb(value), user.email))
        audit(conn, user, "update", "org_settings", "member_sync",
              {k: value.get(k) for k in ("enabled", "file_name", "replace", "min_fraction", "tab_kinds")})
    org.clear()
    return _sync_view()


@router.post("/member-sync/run")
def run_member_sync(user: User = Depends(admin)):
    from .. import sheet_sync
    if not sheet_sync.settings()["enabled"]:
        raise HTTPException(409, "Turn the sync on first")
    r = sheet_sync.run("console")
    with tx() as conn:
        audit(conn, user, "sync", "members", None, {"status": r["status"]})
    return _sync_view()


class ColorIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    meaning: str | None = Field(None, max_length=300)
    effect: str | None = Field(None, pattern=r"^(inactive|type:[a-z0-9_-]{1,30})$")


class LegendIn(BaseModel):
    colors: dict[str, ColorIn]


@router.put("/member-colors")
def put_member_colors(body: LegendIn, user: User = Depends(admin)):
    """Name each color, say what it means, and (optionally) what it does. Applies on the next import."""
    from .. import workbook
    cur = workbook.legend()
    types = {t["key"] for t in (org.get().get("member_types") or {}).get("types") or []}
    for k, c in body.colors.items():
        if k not in cur:
            raise HTTPException(422, f"Unknown color {k}")
        if c.effect and c.effect.startswith("type:") and c.effect[5:] not in types:
            raise HTTPException(422, f"Unknown membership type {c.effect[5:]}")
        cur[k] = {**cur[k], "name": c.name, "meaning": c.meaning, "effect": c.effect, "new": False}
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('member_colors', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                          updated_at = now()""", (Jsonb({"colors": cur}), user.email))
        audit(conn, user, "update", "org_settings", "member_colors", {k: {"meaning": v.meaning, "effect": v.effect}
                                                                       for k, v in body.colors.items()})
    org.clear()
    return _sync_view()


class MemberType(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_-]{1,30}$")
    label: str = Field(min_length=1, max_length=60)
    notes: str | None = Field(None, max_length=500)


class TypesIn(BaseModel):
    types: list[MemberType] = Field(min_length=1, max_length=20)
    default: str


@router.put("/member-types")
def put_member_types(body: TypesIn, user: User = Depends(admin)):
    if body.default not in {t.key for t in body.types}:
        raise HTTPException(422, "The default type must be one of the types")
    value = body.model_dump()
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('member_types', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                          updated_at = now()""", (Jsonb(value), user.email))
        audit(conn, user, "update", "org_settings", "member_types", value)
    org.clear()
    return _sync_view()
