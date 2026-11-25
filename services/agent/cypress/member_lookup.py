"""Member lookup for the board, admins and security (roles.MEMBER_DATA_ROLES; never staff, members or the public): "is 123 Example Lane a member household?".

Privacy rules (see roles.py): members and the public never get this tool, and member records are never
document sets. What each staff role sees is an admin setting:

  details  household name, address, status and names on file (no email addresses, ever)
  confirm  only a yes/no on what the asker already said: an address, a name, or both. It never fills in
           the gaps (an address never reveals a name, a name never reveals an address), and an inactive
           household looks the same as no match ("no active member household found").
  off      no lookups for that role

Plates work the same way (lookup_plate): 'details' gives the vehicle, plate and household; 'confirm' says
whether the plate is registered to an active member household and describes the car (color, make, model) so
a guard can tell if it's the same car, but never the owner or address.

Every lookup is logged (member_lookups) and rate-limited per person, so nobody can walk the street list.
Evals (tools.DRY_RUN) use a small fictional directory instead of the real list.
"""

import logging
import re

from .db import tx

log = logging.getLogger(__name__)

DEFAULTS = {"access": {"security": "confirm", "board": "details", "admin": "details"},
            "per_hour": 30, "per_day": 100}
MODES = ("details", "confirm", "off")
MAX_MATCHES = 5

# Fictional households for evals: never real people or addresses.
FIXTURE = [
    {"id": -1, "name": "Rivera", "address": "123 Example Lane", "status": "active", "people": ["Jordan Rivera", "Sam Rivera"]},
    {"id": -2, "name": "Okafor", "address": "9 Sample Ct", "status": "inactive", "people": ["Chris Okafor"]},
    {"id": -3, "name": "Rivera / Chen", "address": "456 Example Lane", "status": "active", "people": ["Alex Chen"]},
]

SUFFIXES = {"drive": "dr", "lane": "ln", "cove": "cv", "court": "ct", "street": "st", "road": "rd", "circle": "cir",
            "trail": "trl", "boulevard": "blvd", "avenue": "ave", "place": "pl", "parkway": "pkwy", "terrace": "ter",
            "way": "way", "loop": "loop", "path": "path", "bend": "bnd", "cv.": "cv", "dr.": "dr", "ln.": "ln"}
# Fictional vehicles for evals and tests (plates that can't be real Texas plates: they start with "ZZ").
FIXTURE_VEHICLES = [
    {"plate_norm": "ZZT1234", "plate": "ZZT-1234", "state": "TX", "color": "blue", "year": 2019, "make": "Honda",
     "model": "CR-V", "tag": "P-101", "household_id": -1},
    {"plate_norm": "ZZK5678", "plate": "ZZK-5678", "state": "TX", "color": "white", "year": 2015, "make": "Ford",
     "model": "F-150", "tag": None, "household_id": -2},
    {"plate_norm": "ZZT1299", "plate": "ZZT-1299", "state": "TX", "color": "silver", "year": 2021, "make": "Toyota",
     "model": "Camry", "tag": "P-102", "household_id": None},
]
# Fictional cards and tags for evals.
FIXTURE_CARDS = [
    {"kind": "gate_card", "number": "1234567", "household_id": -1, "holder": None},
    {"kind": "gate_card", "number": "5550001", "household_id": None, "holder": "Example Pools LLC"},
    {"kind": "golf_tag", "number": "7000001", "household_id": -1, "holder": None, "detail": "2019 EZGO RXV"},
    {"kind": "sticker", "number": "0101", "household_id": -1, "holder": None, "detail": "blue 2019 Honda CR-V"},
]
DIRECTIONS = {"north": "n", "south": "s", "east": "e", "west": "w"}


def settings() -> dict:
    from . import org
    s = {**DEFAULTS, **(org.get().get("member_lookup") or {})}
    s["access"] = {**DEFAULTS["access"], **(s.get("access") or {})}
    return s


def mode_for(role: str | None) -> str:
    """'details', 'confirm' or 'off'. Members and the public are always off."""
    from . import roles
    if not roles.can_lookup_members(role):
        return "off"
    m = settings()["access"].get(role, "off")
    return m if m in MODES else "off"


def norm_address(a: str | None) -> str:
    """'123 Example Lane, Austin TX 78746' -> '123 example ln'. Unit numbers are kept."""
    if not a:
        return ""
    a = a.lower().split(",")[0]
    a = re.sub(r"\b(austin|texas|tx)\b|\b\d{5}(-\d{4})?\b", " ", a)
    words = re.sub(r"[^a-z0-9# ]", " ", a).split()
    words = [SUFFIXES.get(w, DIRECTIONS.get(w, w)) for w in words]
    return " ".join(words)


def _same_address(q: str, a: str) -> bool:
    """Same house number and street; a missing suffix in the question still matches ('123 example')."""
    if not q or not a or q.split()[0] != a.split()[0] or not q.split()[0].isdigit():
        return False
    return a == q or a.startswith(q + " ") or (len(q.split()) >= 2 and q == " ".join(a.split()[:len(q.split())]))


def _name_tokens(s: str) -> set[str]:
    return {w for w in re.sub(r"[^a-z ]", " ", (s or "").lower()).split() if len(w) > 1 and w not in ("the", "family", "and")}


def _name_matches(q: str, h: dict) -> bool:
    """The asked name's surname appears in the household name or a person's name on file."""
    qt = _name_tokens(q)
    if not qt:
        return False
    surname = sorted(qt, key=lambda w: q.lower().rfind(w))[-1]
    pool = _name_tokens(h["name"]) | set().union(*[_name_tokens(p) for p in h.get("people") or []] or [set()])
    return surname in pool


def _households(dry_run: bool) -> list[dict]:
    if dry_run:
        return FIXTURE
    with tx() as conn:
        return conn.execute(
            """select h.id, h.name, h.address, h.status,
                      coalesce(array_agg(e.name) filter (where e.name is not null), '{}') || h.people as people
               from households h left join member_emails e on e.household_id = h.id
               group by h.id""").fetchall()


def _log(actor, role, channel, address, name, mode, result, matches, dry_run, plate=None) -> None:
    if dry_run:
        return
    with tx() as conn:
        conn.execute("""insert into member_lookups (actor, role, channel, address_q, name_q, mode, result, matches, plate_q)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (actor[:200], role, channel, (address or "")[:200] or None, (name or "")[:200] or None, mode, result,
                      matches, (plate or "")[:20] or None))


def _rate_ok(actor, role, channel, mode, dry_run, plate=None, address=None, name=None) -> str | None:
    """None if under the lookup limit, else the error to return."""
    if dry_run:
        return None
    from .agent import service
    s = settings()
    try:
        service.check_rate(f"lookup:{actor}", "member_lookup",
                           rules=[(3600, int(s["per_hour"])), (86400, int(s["per_day"]))])
    except service.RateLimited as e:
        _log(actor, role, channel, address, name, mode, "rate_limited", 0, dry_run, plate)
        return f"You've reached the member lookup limit for this {e}. Ask CAM, or try again later."
    return None


def _say(result: str, address: str, name: str) -> str:
    """The exact answer for a yes/no lookup, so the wording can't add or drop anything."""
    who = f"{name} at {address}" if address and name else (address or name)
    if result == "active_member":
        return (f"Yes, {name} is on file for {address}, an active member household." if address and name else
                f"Yes, {address} is an active member household." if address else
                f"Yes, there's an active member household for {name}.")
    if result == "name_mismatch":
        return (f"{address} is an active member household, but {name} isn't on file for it. "
                "Check with CAM before treating them as a member.")
    return f"No active member household was found for {who}. CAM can confirm if you think that's wrong."


CONFIRM_HOW = ("Reply with the 'say' sentence (you may add a short, relevant next step). Answer only yes or no about what was asked. Don't add a name, address or anything else about the "
               "household, and don't guess. If there's no active match, say no active member household was found "
               "and suggest checking with CAM.")
DETAILS_HOW = ("Give the household name, address and status for each match. Don't share email addresses or phone "
               "numbers; for contact details, point to the Members page in the admin console or CAM.")


def lookup(*, address: str | None, name: str | None, role: str | None, actor: str, channel: str,
           dry_run: bool = False) -> dict:
    mode = mode_for(role)
    if mode == "off":
        return {"error": "Member lookup isn't available for your role."}
    address, name = (address or "").strip(), (name or "").strip()
    if not address and not name:
        return {"error": "Give a street address, a name, or both."}
    if err := _rate_ok(actor, role, channel, mode, dry_run, address=address, name=name):
        return {"error": err}

    qa = norm_address(address)
    homes = _households(dry_run)
    by_addr = [h for h in homes if qa and _same_address(qa, norm_address(h["address"]))]
    by_name = [h for h in homes if name and _name_matches(name, h)]

    if mode == "confirm":
        if address and name:
            hits = [h for h in by_addr if _name_matches(name, h)]
            active = [h for h in hits if h["status"] == "active"]
            result = "active_member" if active else ("name_mismatch" if any(h["status"] == "active" for h in by_addr) else "not_found")
        else:
            active = [h for h in (by_addr if address else by_name) if h["status"] == "active"]
            result = "active_member" if active else "not_found"
        _log(actor, role, channel, address, name, mode, result, len(active), dry_run)
        out = {"mode": "confirm", "asked": {"address": address or None, "name": name or None},
               "active_member_household": result == "active_member", "say": _say(result, address, name),
               "how_to_answer": CONFIRM_HOW}
        if result == "name_mismatch":
            out["note"] = "That address is an active member household, but the name given isn't on file for it."
        return out

    hits = by_addr if address else by_name
    if address and name:
        both = [h for h in by_addr if _name_matches(name, h)]
        hits = both or by_addr
    hits = sorted(hits, key=lambda h: h["status"] != "active")[:MAX_MATCHES]
    _log(actor, role, channel, address, name, mode, "matches" if hits else "not_found", len(hits), dry_run)
    return {"mode": "details", "asked": {"address": address or None, "name": name or None},
            "matches": [{"household": h["name"], "address": h["address"], "status": h["status"],
                         "people_on_file": list(dict.fromkeys(h.get("people") or []))[:6], **_profile(h["id"], dry_run)}
                        for h in hits],
            "how_to_answer": DETAILS_HOW}


def _profile(hid: int, dry_run: bool) -> dict:
    """More for the details level (board and admins): membership type, CAM's color flags with their meanings,
    vehicles, golf carts and how many gate cards. Never emails, phones or dues."""
    if dry_run or hid is None or hid < 0:
        return {"member_type": "full"} if hid == -1 else {}
    from . import vehicles, workbook
    with tx() as conn:
        h = conn.execute("select member_type, row_color, highlights from households where id = %s", (hid,)).fetchone()
        cars = conn.execute("select * from vehicles where household_id = %s order by plate_norm limit 6", (hid,)).fetchall()
        carts = conn.execute("select tag, description from golf_carts where household_id = %s", (hid,)).fetchall()
        cards = conn.execute("select count(*)::int as n from gate_cards where household_id = %s", (hid,)).fetchone()["n"]
    if not h:
        return {}
    flags = []
    if (m := workbook.color_meaning(h["row_color"])):
        flags.append(f"row marked {m['name']}: {m['meaning'] or 'meaning not set (ask CAM)'}")
    for x in h["highlights"] or []:
        if (m := workbook.color_meaning(x.get("color"))):
            what = "text" if x.get("kind") == "text" else "highlighted"
            flags.append(f"{x['field'].replace('_', ' ')} {what} {m['name']}: {m['meaning'] or 'meaning not set'}")
    return {"member_type": h["member_type"], "cam_flags": flags[:6],
            "vehicles": [f"{vehicles.describe(v)} ({v['plate']})" for v in cars],
            "golf_carts": [f"tag {c['tag']}" + (f": {c['description']}" if c["description"] else "") for c in carts],
            "gate_cards": cards}


PLATE_CONFIRM_HOW = ("Reply with the 'say' sentence. You may describe the car (color, make, model) so the asker can "
                     "check it's the same vehicle, but never give the owner's name, address or household, and don't guess.")
PLATE_DETAILS_HOW = ("For each match give the plate, the car, and the household name, address and status. Say when a match "
                     "is only partial. Don't share email addresses or phone numbers.")


def lookup_plate(*, plate: str, state: str | None = None, role: str | None, actor: str, channel: str,
                 dry_run: bool = False) -> dict:
    """Who a license plate is registered to, at the asker's access level. Partial plates ('ZZT12', 'ZZT?234')
    return up to MAX_MATCHES possible matches."""
    from . import vehicles
    mode = mode_for(role)
    if mode == "off":
        return {"error": "Member lookup isn't available for your role."}
    if len(vehicles.norm_plate(plate)) < 3:
        return {"error": "Give at least 3 characters of the plate."}
    if err := _rate_ok(actor, role, channel, mode, dry_run, plate=plate):
        return {"error": err}
    if dry_run:
        homes = {h["id"]: h for h in FIXTURE}
        hits = [{**v, "household": homes.get(v["household_id"], {}).get("name"),
                 "household_address": homes.get(v["household_id"], {}).get("address"),
                 "household_status": homes.get(v["household_id"], {}).get("status")}
                for v in vehicles.find(plate, state, fixture=FIXTURE_VEHICLES)]
    else:
        hits = vehicles.find(plate, state)
    too_many = len(hits) > MAX_MATCHES
    hits = hits[:MAX_MATCHES]
    partial = any(h["partial"] for h in hits)
    active = [h for h in hits if h.get("household_status") == "active"]
    shown = plate.strip().upper()

    if mode == "confirm":
        if not hits:
            result, say = "not_found", f"No registered vehicle was found for plate {shown}. CAM can confirm if you think that's wrong."
        elif not partial:
            h = hits[0]
            result = "active_member" if active else "not_member"
            say = (f"Yes, plate {shown} is registered to an active member household: a {vehicles.describe(h)}." if active else
                   f"Plate {shown} is on file ({vehicles.describe(h)}), but not for an active member household. Check with CAM.")
        else:
            result = "partial"
            say = (f"{len(hits)}{'+' if too_many else ''} registered vehicle{'s' if len(hits) > 1 else ''} could match "
                   f"{shown}; {len(active)} belong{'s' if len(active) == 1 else ''} to active member households.")
        _log(actor, role, channel, None, None, mode, result, len(hits), dry_run, plate)
        return {"mode": "confirm", "asked": {"plate": shown, "state": state or None}, "say": say,
                "vehicles": [{"car": vehicles.describe(h), "active_member_household": h in active} for h in hits],
                "partial": partial, "how_to_answer": PLATE_CONFIRM_HOW}

    _log(actor, role, channel, None, None, mode, "matches" if hits else "not_found", len(hits), dry_run, plate)
    return {"mode": "details", "asked": {"plate": shown, "state": state or None}, "partial": partial,
            "more_matches": too_many,
            "matches": [{"plate": h["plate"], "state": h["state"] or None, "car": vehicles.describe(h), "tag": h.get("tag"),
                         "household": h.get("household"), "address": h.get("household_address"),
                         "status": h.get("household_status") or "not linked to a household",
                         "exact": not h["partial"]} for h in hits],
            "how_to_answer": PLATE_DETAILS_HOW}


CARD_KINDS = {"gate_card": "gate card", "golf_tag": "golf cart tag", "sticker": "parking sticker"}


def lookup_card(*, kind: str, number: str, role: str | None, actor: str, channel: str, dry_run: bool = False) -> dict:
    """Who a gate card, golf cart tag or parking sticker belongs to, at the asker's access level."""
    mode = mode_for(role)
    if mode == "off":
        return {"error": "Member lookup isn't available for your role."}
    if kind not in CARD_KINDS:
        return {"error": "Say whether it's a gate card, a golf cart tag or a parking sticker."}
    num = re.sub(r"\s+", "", str(number or ""))[:20]
    if len(num) < 3:
        return {"error": "Give the full number."}
    label = CARD_KINDS[kind]
    if err := _rate_ok(actor, role, channel, mode, dry_run, plate=f"{kind}:{num}"):
        return {"error": err}
    hits = []
    if dry_run:
        homes = {h["id"]: h for h in FIXTURE}
        for c in FIXTURE_CARDS:
            if c["kind"] == kind and c["number"].lstrip("0") == num.lstrip("0"):
                h = homes.get(c["household_id"]) or {}
                hits.append({"household": h.get("name"), "address": h.get("address"), "status": h.get("status"),
                             "holder": c["holder"], "detail": c.get("detail")})
    else:
        q = {"gate_card": """select h.name as household, h.address, h.status, g.holder, g.kind as detail from gate_cards g
                              left join households h on h.id = g.household_id where ltrim(g.card, '0') = ltrim(%s, '0')""",
             "golf_tag": """select h.name as household, h.address, h.status, null as holder, c.description as detail from golf_carts c
                             left join households h on h.id = c.household_id where ltrim(c.tag, '0') = ltrim(%s, '0')""",
             "sticker": """select h.name as household, h.address, h.status, null as holder,
                                  concat_ws(' ', v.color, v.year, v.make, v.model) as detail from vehicles v
                            left join households h on h.id = v.household_id where ltrim(v.tag, '0') = ltrim(%s, '0')"""}[kind]
        with tx() as conn:
            hits = conn.execute(q + " limit 5", (num,)).fetchall()
    active = [h for h in hits if h.get("status") == "active"]
    vendor = next((h for h in hits if h.get("holder")), None)
    if mode == "confirm":
        if active:
            result, say = "active_member", f"Yes, {label} {num} belongs to an active member household."
        elif vendor:
            result, say = "vendor", f"{label.capitalize()} {num} is a vendor card ({vendor['holder']})."
        elif hits:
            result, say = "not_member", f"{label.capitalize()} {num} is on file, but not for an active member household. Check with CAM."
        else:
            result, say = "not_found", f"{label.capitalize()} {num} isn't on file. CAM can confirm if you think that's wrong."
        _log(actor, role, channel, None, None, mode, result, len(hits), dry_run, f"{kind}:{num}")
        out = {"mode": "confirm", "asked": {"kind": label, "number": num}, "say": say, "how_to_answer": CONFIRM_HOW}
        if kind in ("sticker", "golf_tag") and active and active[0].get("detail"):
            out["describes"] = active[0]["detail"]          # the car or cart it belongs on, so a guard can check
        return out
    _log(actor, role, channel, None, None, mode, "matches" if hits else "not_found", len(hits), dry_run, f"{kind}:{num}")
    return {"mode": "details", "asked": {"kind": label, "number": num},
            "matches": [{"household": h.get("household"), "address": h.get("address"),
                         "status": h.get("status") or ("vendor" if h.get("holder") else "not linked to a household"),
                         "vendor": h.get("holder"), "detail": h.get("detail") if h.get("detail") not in ("member", "vendor") else None}
                        for h in hits],
            "how_to_answer": "Give whose it is: the household, address and status, or the vendor. Don't share emails or phone numbers."}


def recent(limit: int = 100) -> list[dict]:
    with tx() as conn:
        return conn.execute("select * from member_lookups order by at desc limit %s", (limit,)).fetchall()
