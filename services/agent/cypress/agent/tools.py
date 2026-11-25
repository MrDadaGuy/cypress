"""Agent tools, gated by role.

One agent, different tools per caller. Three layers keep that safe:
  1. for_role(role) decides which tools the model is even shown for this request.
  2. call() re-checks the role before running anything, so a tool the model wasn't offered
     (or was talked into naming) still can't run.
  3. Tools receive the caller's identity from the server (Ctx), never from model arguments, and
     return only the fields the answer needs.
"""

import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .. import roles

log = logging.getLogger(__name__)

EVERYONE = (None, *roles.ROLES)

# Evals run real questions through the agent; tools with side effects pretend instead of acting.
import contextvars  # noqa: E402
DRY_RUN: contextvars.ContextVar[bool] = contextvars.ContextVar("tools_dry_run", default=False)


@dataclass(frozen=True)
class Ctx:
    role: str | None
    uid: str | None
    channel: str
    email: str | None = None
    attachments: tuple[str, ...] = ()   # photos sent with this message (server-side, not model args)

    @property
    def actor(self) -> str:
        return self.email or self.uid or f"anonymous {self.channel} visitor"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict           # JSON schema for the model's arguments
    roles: tuple               # who may use it (None = anonymous public)
    fn: Callable[[dict, Ctx], dict]
    status: str = "Looking that up…"   # shown to the user while it runs
    available: Callable[[str | None], bool] | None = None   # extra per-role check (admin settings)

    def allows(self, role: str | None) -> bool:
        return role in self.roles and (self.available is None or self.available(role))


# ---------------- deeded access / boundary check ----------------

BOUNDARIES_URL = "https://boundaries.lakehillsca.org/api/lhca_boundary_check"
NEAR_EDGE_M = 60          # closer than this to the outer boundary = borderline, check with the board
FAR_AWAY_M = 5000         # this far away = clearly not Lake Hills (often a typo or another city)
CAVEAT = ("This check uses the geocoded center point of the property. A few lots straddle survey lines "
          "(for example 10212 Birdlip Circle, partly in the Tyler Tap #40 survey), and the association "
          "generally treats those as having deeded access. The deed itself is authoritative.")


def _metres(deg: float, lat: float) -> float:
    # Good enough near Austin for "how far from the line" (not for surveying).
    return deg * 111_320 * math.sqrt((math.cos(math.radians(lat)) ** 2 + 1) / 2)


def check_deeded_access(args: dict, ctx: Ctx) -> dict:
    from shapely.geometry import Point, shape

    address = re.sub(r"\s+", " ", str(args.get("address", ""))).strip()[:200]
    if len(address) < 5:
        return {"error": "Please give a street address, like 3106 Edgewater Dr."}
    if not re.search(r"\b(TX|Texas|787\d\d)\b", address, re.I):
        address += ", Austin, TX 78733"
    try:
        r = httpx.get(BOUNDARIES_URL, params={"address": address}, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("boundary service failed: %s", e)
        return {"error": "The boundary service didn't respond. Try boundaries.lakehillsca.org directly."}
    if data.get("error") or "coords" not in data:
        return {"error": "I couldn't find that address on the map. Check the spelling or add the ZIP code."}

    lng, lat = data["coords"]
    point = Point(lng, lat)
    out = {"address_checked": data.get("address", address), "inside_survey_area": bool(data.get("inside")),
           "survey_area": data.get("name"), "map": "https://boundaries.lakehillsca.org/check-address?address=" + quote(address),
           "caveat": CAVEAT}
    try:
        outer = data.get("outer_boundary") or {}
        geoms = [shape(f["geometry"]) for f in outer.get("features", [])] or ([shape(outer)] if outer.get("type") else [])
        if geoms:
            dist = min(_metres(g.exterior.distance(point) if hasattr(g, "exterior") else g.boundary.distance(point), lat)
                       for g in geoms)
            out["metres_from_outer_boundary"] = round(dist)
            if not out["inside_survey_area"] and dist > FAR_AWAY_M:
                out["note"] = (f"This address is about {dist / 1000:.0f} km from Lake Hills, so it is not in the LHCA "
                               "area. If a Lake Hills street was meant, ask for the full address with ZIP 78733.")
                out.pop("caveat")
            out["borderline"] = dist < NEAR_EDGE_M
    except Exception:
        log.exception("distance to boundary failed")
    return out


# ---------------- tickets ----------------

STAFF = ("staff", "security", "board", "admin")
MEMBER_DATA = roles.MEMBER_DATA_ROLES        # membership data: board, admin and security only


def report_issue(args: dict, ctx: Ctx) -> dict:
    from .. import members, notify, tickets
    if DRY_RUN.get():
        needs = ctx.role is None and members.auth_settings().get("require_for_reports")
        return ({"filed": False, "needs_member_sign_in": True, "pending_id": "00000000-0000-0000-0000-000000000000",
                 "draft_title": args.get("title"), "instructions": "Not filed yet. Tell them it's drafted "
                 "and to confirm with the sign-in box below; it's filed when they open the emailed link."} if needs else
                {"filed": True, "ticket_number": 9999, "status": "new", "priority": args.get("urgency") or "normal",
                 "photos_attached": 0, "board_notified": True})   # must look real to the model
    if ctx.role is None and ctx.channel == "web" and members.auth_settings().get("require_for_reports"):
        # Keep the draft; the resident confirms they're a member (emailed link) and it's filed then.
        payload = {k: (str(args[k])[:4000 if k == "description" else 200] if args.get(k) else None)
                   for k in ("title", "description", "location", "category", "urgency", "contact")}
        pid = members.create_pending("report_issue", payload, list(ctx.attachments), ctx.channel)
        return {"filed": False, "needs_member_sign_in": True, "pending_id": pid, "draft_title": payload["title"],
                "instructions": ("Not filed yet. Tell the resident their report is drafted and that members confirm "
                                 "with their email address using the sign-in box shown below this message; it is "
                                 "filed as soon as they open the emailed link. Don't ask for the email in chat.")}
    try:
        t = tickets.create(
            role=ctx.role, actor=ctx.actor, channel=ctx.channel if ctx.channel in ("web", "slack") else "web",
            title=str(args.get("title", ""))[:200], description=str(args.get("description", ""))[:4000],
            category=args.get("category") or "other", priority=args.get("urgency") or "normal",
            location=args.get("location") or None, reporter_uid=ctx.uid,
            reporter_contact=(str(args["contact"])[:200] if args.get("contact") else None))
    except tickets.TicketsError as e:
        return {"error": f"The report couldn't be filed: {e.detail}"}
    from .. import uploads
    photos = uploads.attach(list(ctx.attachments), t["id"])
    notified = notify.new_ticket(t, photos=photos)
    return {"filed": True, "ticket_number": t["id"], "status": t["status"], "priority": t["priority"],
            "photos_attached": photos, "board_notified": notified}


def list_tickets(args: dict, ctx: Ctx) -> dict:
    from .. import tickets
    rows = tickets.list_tickets(role=ctx.role, actor=ctx.actor, status=args.get("status") or "open", limit=25)
    return {"tickets": [{k: t.get(k) for k in ("id", "title", "status", "priority", "category", "location",
                                               "assignee", "created_at")} for t in rows],
            "console": "https://lhca-cypress.web.app/admin/tickets"}


def update_ticket(args: dict, ctx: Ctx) -> dict:
    from .. import notify, tickets
    if DRY_RUN.get():
        return {"id": args.get("ticket_id"), "title": "Ticket", "status": args.get("status") or "new",
                "priority": args.get("priority") or "normal", "assignee": args.get("assignee")}
    try:
        t = tickets.update(int(args["ticket_id"]), role=ctx.role, actor=ctx.actor, status=args.get("status"),
                           priority=args.get("priority"), assignee=args.get("assignee"), note=args.get("note"))
    except (tickets.TicketsError, KeyError, ValueError) as e:
        return {"error": getattr(e, "detail", "Give a ticket number.")}
    notify.refresh(t, f"Updated by {ctx.actor} via Cypress")
    return {k: t.get(k) for k in ("id", "title", "status", "priority", "assignee")}


def lookup_member(args: dict, ctx: Ctx) -> dict:
    from .. import member_lookup
    return member_lookup.lookup(address=args.get("address"), name=args.get("name"), role=ctx.role, actor=ctx.actor,
                                channel=ctx.channel, dry_run=DRY_RUN.get())


def file_incident(args: dict, ctx: Ctx) -> dict:
    from .. import cases, notify, tickets, uploads

    def items(k):
        v = args.get(k) or []
        v = [v] if isinstance(v, str) else v
        return [str(x).strip()[:120] for x in v if str(x).strip()][:10]
    report = {"title": str(args.get("title") or "Security incident")[:200], "what": str(args.get("what") or "")[:3000],
              "where": str(args.get("where") or "")[:200] or None, "when": str(args.get("when") or "")[:100] or None,
              "plates": items("plates"), "names": items("names"), "addresses": items("addresses"),
              "vehicle": str(args.get("vehicle") or "")[:300] or None, "people": str(args.get("people") or "")[:300] or None}
    if DRY_RUN.get():
        return {"filed": True, "ticket_number": 9998, "case_file": "queued", "photos_attached": len(ctx.attachments)}
    desc = report["what"] + "".join(f"\n{label}: {', '.join(v) if isinstance(v, list) else v}"
                                    for label, v in (("When", report["when"]), ("Plates", report["plates"]),
                                                     ("Names given", report["names"]), ("Addresses", report["addresses"]),
                                                     ("Vehicle", report["vehicle"]), ("People", report["people"])) if v)
    try:
        t = tickets.create(role=ctx.role, actor=ctx.actor, channel=ctx.channel if ctx.channel in ("web", "slack") else "web",
                           title=report["title"], description=desc[:4000], category="security",
                           priority=args.get("urgency") or "normal", location=report["where"], reporter_uid=ctx.uid,
                           reporter_contact=None)
    except tickets.TicketsError as e:
        return {"error": f"The incident couldn't be filed: {e.detail}"}
    photos = uploads.attach(list(ctx.attachments), t["id"])
    cases.open_case(t["id"], report, list(ctx.attachments), ctx.actor)
    notify.new_ticket(t, photos=photos)
    return {"filed": True, "ticket_number": t["id"], "photos_attached": photos,
            "case_file": "Cypress is building a case file for the board; it appears on the ticket in a few minutes."}


def lookup_plate(args: dict, ctx: Ctx) -> dict:
    from .. import member_lookup
    return member_lookup.lookup_plate(plate=str(args.get("plate") or ""), state=args.get("state"), role=ctx.role,
                                      actor=ctx.actor, channel=ctx.channel, dry_run=DRY_RUN.get())


def lookup_card(args: dict, ctx: Ctx) -> dict:
    from .. import member_lookup
    return member_lookup.lookup_card(kind=str(args.get("kind") or ""), number=str(args.get("number") or ""), role=ctx.role,
                                     actor=ctx.actor, channel=ctx.channel, dry_run=DRY_RUN.get())


def _lookup_on(role: str | None) -> bool:
    from .. import member_lookup
    return member_lookup.mode_for(role) != "off"


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool(
        name="check_deeded_access",
        description=("Check whether a street address in the Lake Hills / Cuernavaca area falls inside the original "
                     "surveys that carry deeded access to the LHCA park. Use for questions like 'does 3106 Edgewater "
                     "Dr have deeded access?' or 'is my house in the LHCA boundary?'. Public map data only: it "
                     "says nothing about who lives there or whether they are a member."),
        parameters={"type": "object", "properties": {
            "address": {"type": "string", "description": "Street address, e.g. '3106 Edgewater Dr' (city/ZIP optional)"}},
            "required": ["address"]},
        roles=EVERYONE,
        fn=check_deeded_access,
        status="Checking the boundary map…",
    ),
    Tool(
        name="report_issue",
        description=("File an issue ticket for the LHCA board and staff: something broken, damaged, unsafe, dirty, "
                     "or a rule being broken at the park, pool, clubhouse or grounds. Only file when the person is "
                     "reporting a real problem, and you know what it is and roughly where."),
        parameters={"type": "object", "properties": {
            "title": {"type": "string", "description": "Short summary, e.g. 'Pool gate does not latch'"},
            "description": {"type": "string", "description": "What's wrong, in the reporter's words plus any details given"},
            "location": {"type": "string", "description": "Where, e.g. 'pool entrance', 'kayak racks', 'clubhouse kitchen'"},
            "category": {"type": "string", "enum": ["maintenance", "safety", "rules", "pool", "grounds", "other"]},
            "urgency": {"type": "string", "enum": ["low", "normal", "high", "urgent"],
                        "description": "urgent = safety hazard now; high = facility unusable; normal otherwise"},
            "contact": {"type": "string", "description": "Optional email or phone the reporter offered for follow-up"}},
            "required": ["title", "description", "category", "urgency"]},
        roles=EVERYONE,
        fn=report_issue,
        status="Filing your report…",
    ),
    Tool(
        name="file_incident",
        description=("File a security incident (a person or vehicle involved: trespassing, parking, rule breaking, a "
                     "suspicious vehicle, damage by someone). Cypress then builds a case file for the board: reads plates "
                     "in attached photos, matches plates, names and addresses to households, finds earlier incidents and "
                     "the rules that apply. Pass everything the reporter gave, exactly as given."),
        parameters={"type": "object", "properties": {
            "title": {"type": "string", "description": "Short summary, e.g. 'Car parked in the boat ramp lane overnight'"},
            "what": {"type": "string", "description": "What happened, in the reporter's words"},
            "where": {"type": "string"}, "when": {"type": "string", "description": "When it happened, as given"},
            "plates": {"type": "array", "items": {"type": "string"}, "description": "Plates, full or partial, as given"},
            "names": {"type": "array", "items": {"type": "string"}, "description": "Names people gave"},
            "addresses": {"type": "array", "items": {"type": "string"}, "description": "Addresses people gave"},
            "vehicle": {"type": "string", "description": "Vehicle description, e.g. 'white Ford pickup'"},
            "people": {"type": "string", "description": "How many people and what they were doing (no physical descriptions needed)"},
            "urgency": {"type": "string", "enum": ["low", "normal", "high", "urgent"]}},
            "required": ["title", "what"]},
        roles=("security", "staff", "board", "admin"),
        fn=file_incident,
        status="Filing the incident…",
    ),
    Tool(
        name="list_tickets",
        description="List issue tickets (default: open ones), most urgent first.",
        parameters={"type": "object", "properties": {
            "status": {"type": "string", "enum": ["open", "all", "new", "acknowledged", "in_progress", "resolved", "closed"]}}},
        roles=STAFF,
        fn=list_tickets,
        status="Checking tickets…",
    ),
    Tool(
        name="update_ticket",
        description="Change a ticket's status, priority or assignee, optionally with a note.",
        parameters={"type": "object", "properties": {
            "ticket_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["new", "acknowledged", "in_progress", "resolved", "closed"]},
            "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
            "assignee": {"type": "string"},
            "note": {"type": "string"}},
            "required": ["ticket_id"]},
        roles=STAFF,
        fn=update_ticket,
        status="Updating the ticket…",
    ),
    Tool(
        name="lookup_member",
        description=("Check the LHCA membership list: whether an address or person is an active member household. "
                     "Use for questions like 'is 123 Example Lane a member?', 'is Pat Smith a member?' or 'who lives "
                     "at ...?'. Pass exactly what the person asked about. Depending on the asker's role it returns "
                     "either details or only a yes/no; follow the result's how_to_answer. For license plates use "
                     "lookup_plate. It has no phone or dues-balance data."),
        parameters={"type": "object", "properties": {
            "address": {"type": "string", "description": "Street address as given, e.g. '123 Example Lane'"},
            "name": {"type": "string", "description": "Person or household name as given, e.g. 'Pat Smith'"}}},
        roles=MEMBER_DATA,
        fn=lookup_member,
        status="Checking the member list…",
        available=_lookup_on,
    ),
    Tool(
        name="lookup_plate",
        description=("Check whether a license plate is registered to a member household. Use for 'whose car is "
                     "ABC-1234?', 'is plate ZZT1234 a resident's?' or a partial plate ('starts ZZT12', 'ZZT?234'). "
                     "Pass the plate exactly as given. Depending on the asker's role it returns the household or only "
                     "whether it's a member's car plus the car's description; follow the result's how_to_answer."),
        parameters={"type": "object", "properties": {
            "plate": {"type": "string", "description": "Plate as given; use ? for unknown characters"},
            "state": {"type": "string", "description": "Plate state if given, e.g. 'TX'"}},
            "required": ["plate"]},
        roles=MEMBER_DATA,
        fn=lookup_plate,
        status="Checking registered vehicles…",
        available=_lookup_on,
    ),
    Tool(
        name="lookup_card",
        description=("Find who a gate card, golf cart tag or parking sticker number belongs to: 'whose gate card is 1234567?', "
                     "'golf tag 7000001', 'sticker 0101'. Depending on the asker's role it returns the household or only "
                     "whether it's a member's (or a vendor's); follow the result's how_to_answer."),
        parameters={"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["gate_card", "golf_tag", "sticker"]},
            "number": {"type": "string", "description": "The number as given"}},
            "required": ["kind", "number"]},
        roles=MEMBER_DATA,
        fn=lookup_card,
        status="Checking cards and tags…",
        available=_lookup_on,
    ),
]}


def for_role(role: str | None) -> list[Tool]:
    return [t for t in TOOLS.values() if t.allows(role)]


def call(name: str, args: dict, ctx: Ctx) -> dict:
    tool = TOOLS.get(name)
    if not tool or not tool.allows(ctx.role):
        log.warning("blocked tool %s for role %s", name, ctx.role)
        return {"error": f"The {name} tool isn't available to you."}
    try:
        return tool.fn(args or {}, ctx)
    except Exception:
        log.exception("tool %s failed", name)
        return {"error": "That lookup failed on my side. Please try again."}
