"""Vehicles on member households: CSV import (until the Vantaca sync exists) and plate matching.

Admin-only data, like the membership list. Plates are matched on a normalized form (letters and digits,
upper case), so "abc-1234", "ABC 1234" and "ABC1234" are the same plate. Each vehicle is linked to its
household by the account number in the file, or else by address (the same matching member lookup uses).
Lookups go through member_lookup.lookup_plate, which applies each role's access level and logs every query.
"""

import re

from .db import tx
from .member_lookup import _same_address, norm_address

ALIASES = {
    "plate": ("plate", "license plate", "license", "plate #", "plate number", "license plate number", "tag number",
              "lic plate", "license #", "registration"),
    "state": ("state", "plate state", "license state", "registration state", "st"),
    "make": ("make", "vehicle make", "manufacturer"),
    "model": ("model", "vehicle model"),
    "color": ("color", "colour", "vehicle color"),
    "year": ("year", "vehicle year", "model year"),
    "tag": ("sticker", "sticker #", "permit", "permit #", "parking permit", "decal", "decal #", "gate tag", "tag",
            "hang tag", "pass"),
    "account_ref": ("account", "account #", "account number", "member id", "id", "lot", "membership #",
                    "membership number", "unit"),
    "address": ("address", "street address", "property address", "street"),
    "owner": ("owner", "name", "household", "resident", "member", "owner name"),
    "notes": ("notes", "note", "comments", "comment"),
}
FIELDS = tuple(ALIASES)
STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
          "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
          "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"}
STATE_NAMES = {"texas": "TX", "california": "CA", "oklahoma": "OK", "louisiana": "LA", "new mexico": "NM",
               "arkansas": "AR", "colorado": "CO", "florida": "FL", "arizona": "AZ"}


def norm_plate(p: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (p or "").upper())[:10]


def norm_state(s: str | None) -> str:
    s = (s or "").strip()
    return s.upper() if s.upper() in STATES else STATE_NAMES.get(s.lower(), "")


def guess_mapping(headers: list[str]) -> dict[str, str | None]:
    norm = {h: re.sub(r"\s+", " ", h.strip().lower().replace("_", " ")) for h in headers}
    used, out = set(), {}
    for field in FIELDS:
        out[field] = next((h for h in headers if h not in used and norm[h] in ALIASES[field]), None)
        if out[field]:
            used.add(out[field])
    return out


def _households() -> list[dict]:
    with tx() as conn:
        return conn.execute("select id, name, address, account_ref, status from households").fetchall()


def match_household(account_ref: str | None, address: str | None, homes: list[dict]) -> dict | None:
    if account_ref:
        h = next((h for h in homes if (h["account_ref"] or "").strip().lower() == account_ref.strip().lower()), None)
        if h:
            return h
    qa = norm_address(address)
    if qa:
        hits = [h for h in homes if _same_address(qa, norm_address(h["address"]))]
        if len(hits) == 1 or (hits and len({h["id"] for h in hits}) == 1):
            return hits[0]
        active = [h for h in hits if h["status"] == "active"]
        if len(active) == 1:
            return active[0]
    return None


def normalize(rows: list[dict], mapping: dict[str, str | None], homes: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    """Rows -> vehicles, each with the household it matched (or None). Returns (vehicles, problems)."""
    homes = _households() if homes is None else homes
    get = lambda r, f: (r.get(mapping.get(f) or "", "") or "").strip()
    out, problems, seen = [], [], {}
    for i, r in enumerate(rows, start=2):
        raw = get(r, "plate")
        plate = norm_plate(raw)
        if not plate:
            if any(get(r, f) for f in ("make", "model", "address", "account_ref")):
                problems.append(f"Line {i}: no plate number; skipped")
            continue
        state = norm_state(get(r, "state"))
        if get(r, "state") and not state:
            problems.append(f"Line {i}: '{get(r, 'state')}' isn't a US state; plate kept without a state")
        key = (plate, state)
        if key in seen:
            problems.append(f"Line {i}: plate {raw} is also on line {seen[key]}; kept the later one")
            out = [v for v in out if (v["plate_norm"], v["state"]) != key]
        seen[key] = i
        year = get(r, "year")
        year = int(year) if year.isdigit() and 1900 <= int(year) <= 2100 else None
        h = match_household(get(r, "account_ref") or None, get(r, "address") or None, homes)
        if not h:
            problems.append(f"Line {i}: plate {raw} didn't match a household (account or address)")
        out.append({"line": i, "plate": raw[:20], "plate_norm": plate, "state": state, "make": get(r, "make")[:40] or None,
                    "model": get(r, "model")[:40] or None, "color": get(r, "color")[:30] or None, "year": year,
                    "tag": get(r, "tag")[:40] or None, "notes": get(r, "notes")[:300] or None,
                    "account_ref": get(r, "account_ref")[:100] or None, "address": get(r, "address")[:300] or None,
                    "household_id": h["id"] if h else None, "household": h["name"] if h else None})
    return out, problems


def commit_import(vehicles: list[dict], *, replace: bool, source: str) -> dict:
    """Upsert by plate and state. replace=True removes vehicles that aren't in this file."""
    stats = {"created": 0, "updated": 0, "removed": 0, "unmatched": sum(1 for v in vehicles if not v["household_id"])}
    ids = []
    with tx() as conn:
        for v in vehicles:
            r = conn.execute(
                """insert into vehicles (household_id, plate, plate_norm, state, make, model, color, year, tag, notes,
                                         account_ref, address, source)
                   values (%(household_id)s, %(plate)s, %(plate_norm)s, %(state)s, %(make)s, %(model)s, %(color)s,
                           %(year)s, %(tag)s, %(notes)s, %(account_ref)s, %(address)s, %(source)s)
                   on conflict (plate_norm, state) do update set household_id = excluded.household_id,
                     plate = excluded.plate, make = excluded.make, model = excluded.model, color = excluded.color,
                     year = excluded.year, tag = excluded.tag, notes = excluded.notes, account_ref = excluded.account_ref,
                     address = excluded.address, source = excluded.source, updated_at = now()
                   returning id, (xmax = 0) as inserted""", {**v, "source": source}).fetchone()
            ids.append(r["id"])
            stats["created" if r["inserted"] else "updated"] += 1
        if replace:
            stats["removed"] = conn.execute("delete from vehicles where not (id = any(%s))", (ids,)).rowcount
    return stats


def relink() -> int:
    """After a member import, link unmatched vehicles whose account or address now matches a household."""
    homes = _households()
    with tx() as conn:
        loose = conn.execute("select id, account_ref, address from vehicles where household_id is null").fetchall()
        n = 0
        for v in loose:
            h = match_household(v["account_ref"], v["address"], homes)
            if h:
                conn.execute("update vehicles set household_id = %s, updated_at = now() where id = %s", (h["id"], v["id"]))
                n += 1
    return n


def describe(v: dict) -> str:
    """'blue 2019 Honda CR-V'."""
    return " ".join(str(x) for x in (v.get("color"), v.get("year"), v.get("make"), v.get("model")) if x) or "vehicle"


def find(plate: str, state: str | None = None, *, fixture: list[dict] | None = None, limit: int = 5) -> list[dict]:
    """Vehicles matching a plate. Exact first; otherwise partial: '?' or '*' stand for unknown characters,
    and 4 or more known characters in a row match anywhere in the plate."""
    raw = (plate or "").upper()
    exact = norm_plate(raw)
    pattern = re.sub(r"[^A-Z0-9?*]", "", raw).replace("*", "%").replace("?", "_")
    st = norm_state(state)
    rows = fixture if fixture is not None else None

    def ok_state(v):
        return not st or not v["state"] or v["state"] == st

    if rows is not None:
        hits = [v for v in rows if v["plate_norm"] == exact and ok_state(v)]
        if not hits and ("_" in pattern or "%" in pattern or len(exact) >= 4):
            rx = re.compile("^" + (pattern if ("_" in pattern or "%" in pattern) else f"%{exact}%")
                            .replace("%", ".*").replace("_", ".") + "$")
            hits = [v for v in rows if rx.match(v["plate_norm"]) and ok_state(v)]
        return [{**v, "partial": v["plate_norm"] != exact} for v in hits[:limit + 1]]
    q = """select v.*, h.name as household, h.address as household_address, h.status as household_status
           from vehicles v left join households h on h.id = v.household_id
           where {cond} and (%(st)s = '' or v.state in ('', %(st)s)) order by v.plate_norm limit %(n)s"""
    with tx() as conn:
        hits = conn.execute(q.format(cond="v.plate_norm = %(p)s"), {"p": exact, "st": st, "n": limit + 1}).fetchall()
        if not hits and ("_" in pattern or "%" in pattern or len(exact) >= 4):
            like = pattern if ("_" in pattern or "%" in pattern) else f"%{exact}%"
            hits = conn.execute(q.format(cond="v.plate_norm like %(p)s"), {"p": like, "st": st, "n": limit + 1}).fetchall()
    return [{**v, "partial": v["plate_norm"] != exact} for v in hits]
