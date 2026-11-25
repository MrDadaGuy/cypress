"""Security incident case files: a supervisor and sub-agents, then the board decides.

A guard, staff or board member files an incident with whatever they know (photos, a plate or part of one,
names, an address, what happened). The ingest worker then builds the case file with this LangGraph:

    supervisor ──► plate_reader ──┬──► matcher ──► history ──┐
     (plans)       (vision; only  │                          ├──► writer ──► board ──► finalize
                    with photos)  └──► policy ───────────────┘              (pauses until the
                                                                              board decides)

Each sub-agent has only what it needs (least privilege):
  plate_reader  the photos; no member data. Never identifies people from faces or appearance.
  matcher       a tool-calling agent with plate and member lookup only (logged, rate-limited, details level).
  history       earlier cases and tickets for the same households or plates (SQL, no model).
  policy        searches bylaws, rules and policies and names the rule and the process the board must follow.
  writer        writes the board's summary and, when a member household matched and a rule applies, a draft
                notice letter. Contact details are added by code, never by the model.
The graph pauses at "board" (a LangGraph interrupt, checkpointed in Postgres). With a draft letter, Cypress opens a
board vote (votes.py: Slack buttons and the console; the rule is an admin setting, a majority of the seated board
by default). A passing vote approves the letter as ready for CAM; a failed or expired vote dismisses it. Without a
letter, one board member closes the case out. Cypress never sends the letter itself.

Who sees what (org_settings 'cases'): the full case file goes to full_roles (board and admin by default).
Security also sees whether a member household matched; staff see only the status (membership data is for
roles.MEMBER_DATA_ROLES).
"""

import json
import logging
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.types.json import Jsonb

from . import models, tracing
from .db import tx

log = logging.getLogger(__name__)

AGENTS = ("plate_reader", "matcher", "history", "policy")
DEFAULTS = {"full_roles": ["board", "admin"], "vision": True, "draft_letters": True, "policy_reasoning": "linear"}
MAX_PHOTOS = 4
MATCH_ROUNDS = 8
ACTOR = "cypress case file"


def settings() -> dict:
    from . import org
    return {**DEFAULTS, **(org.get().get("cases") or {})}


def _merge(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class CaseState(TypedDict, total=False):
    case_id: int
    ticket_id: int
    report: dict                 # title, what, where, when, plates, names, addresses, vehicle
    attachments: list[str]
    dry_run: bool                # evals/tests: fictional directory, no database writes for lookups
    plan: dict
    findings: Annotated[dict, _merge]
    households: list[dict]       # matched households (id, name, address, status, confidence, basis)
    profile: dict
    letter: dict | None
    decision: dict
    steps: Annotated[list[str], operator.add]


def _report_text(r: dict) -> str:
    lines = [f"{k.replace('_', ' ').capitalize()}: {v if isinstance(v, str) else ', '.join(map(str, v))}"
             for k, v in r.items() if v and k in ("title", "what", "where", "when", "plates", "names", "addresses", "vehicle", "people")]
    return "\n".join(lines)


# ---------------- supervisor ----------------

def plan_model(report: dict, photos: int) -> dict | None:
    """The supervisor model's own plan, before the code's guardrails (evals score this)."""
    text, _ = models.generate("case_plan", _system("case_plan"), [{"role": "user", "content":
                              f"{_report_text(report)}\nPhotos attached: {photos}"}], json_mode=True, max_tokens=200, temperature=0)
    j = models.parse_json(text)
    agents = [a for a in j.get("agents") or [] if a in AGENTS]
    return {"agents": agents, "reason": str(j.get("reason") or "")[:200]} if agents else None


def guard_plan(plan: dict, report: dict, photos: int) -> dict:
    """Guardrails on the plan: the code, not the model, has the last word."""
    identifying = bool(report.get("plates") or report.get("names") or report.get("addresses") or photos)
    agents = set(plan["agents"]) | {"policy"}
    if not photos or not settings()["vision"]:
        agents.discard("plate_reader")
    if not identifying:
        agents -= {"matcher", "history"}
    if "matcher" in agents:
        agents.add("history")
    return {**plan, "agents": [a for a in AGENTS if a in agents]}


def supervisor(state: CaseState) -> dict:
    r, photos = state["report"], len(state.get("attachments") or [])
    plan = {"agents": ["plate_reader", "matcher", "history", "policy"] if photos else ["matcher", "history", "policy"],
            "reason": "default plan"}
    try:
        plan = plan_model(r, photos) or plan
    except Exception:
        log.warning("case plan failed; using the default plan", exc_info=True)
    plan = guard_plan(plan, r, photos)
    tracing.set_attrs(**{"cypress.agents": ",".join(plan["agents"])})
    return {"plan": plan, "steps": ["supervisor"]}


def _system(key: str) -> str:
    from .agent import prompts
    return prompts.system(key)


# ---------------- plate reader (vision) ----------------

def plate_reader(state: CaseState) -> dict:
    if "plate_reader" not in state["plan"]["agents"]:
        return {}
    from . import uploads
    with tx() as conn:
        rows = conn.execute("""select id::text, content_type from uploads where id = any(%s::uuid[]) and kind = 'photo'
                               and removed_at is null""", (list(state.get("attachments") or []),)).fetchall()
    plates, vehicles, notes, read = [], [], [], 0
    for row in rows[:MAX_PHOTOS]:
        data = uploads.read(row["id"])
        if not data:
            continue
        j = read_photo(data, row["content_type"] or "image/jpeg")
        if j is None:
            continue
        read += 1
        plates += j["plates"]
        vehicles += j["vehicles"]
        if j["notes"]:
            notes.append(j["notes"])
    return {"findings": {"plate_reader": {"photos_read": read, "plates": plates, "vehicles": vehicles[:6],
                                          "notes": " ".join(notes)[:600]}}, "steps": ["plate_reader"]}


def read_photo(data: bytes, content_type: str) -> dict | None:
    """One photo through the vision model, cleaned up: {plates, vehicles, notes}, or None if it couldn't be read."""
    j = None
    for attempt in (1, 2):            # vision calls occasionally time out or come back cut short: one retry
        try:
            text, _ = models.generate_vision("plate_read", _system("plate_read"), "Read this photo.",
                                             [(data, content_type)], max_tokens=4096)
            j = models.parse_json(text)
            break
        except Exception:
            log.warning("plate reading failed for an upload (attempt %s)", attempt, exc_info=True)
    if j is None:
        return None
    plates = [{"text": str(p["text"])[:12], "state": str(p.get("state") or "")[:20],
               "confidence": p.get("confidence") if p.get("confidence") in ("high", "medium", "low") else "low"}
              for p in j.get("plates") or [] if isinstance(p, dict) and str(p.get("text") or "").strip()]
    vehicles = [{k: str(v.get(k) or "")[:30] for k in ("color", "make", "model", "type")}
                for v in j.get("vehicles") or [] if isinstance(v, dict)]
    return {"plates": plates, "vehicles": vehicles, "notes": str(j.get("notes") or "")[:300]}


# ---------------- matcher (tool-calling sub-agent) ----------------

def _match_tools(state: CaseState):
    from . import member_lookup
    from .agent.tools import Tool
    dry = bool(state.get("dry_run"))
    actor = f"{ACTOR} (ticket #{state['ticket_id']})"
    seen: list[dict] = []

    def plate(args, _ctx=None):
        r = member_lookup.lookup_plate(plate=str(args.get("plate") or ""), state=args.get("state"), role="admin",
                                       actor=actor, channel="case", dry_run=dry)
        seen.append(r)
        return r

    def member(args, _ctx=None):
        r = member_lookup.lookup(address=args.get("address"), name=args.get("name"), role="admin", actor=actor,
                                 channel="case", dry_run=dry)
        seen.append(r)
        return r

    tools = [
        Tool("lookup_plate", "Find who a license plate (full or partial, ? for unknown characters) is registered to.",
             {"type": "object", "properties": {"plate": {"type": "string"}, "state": {"type": "string"}}, "required": ["plate"]},
             ("admin",), plate),
        Tool("lookup_member", "Find member households by street address, name, or both.",
             {"type": "object", "properties": {"address": {"type": "string"}, "name": {"type": "string"}}},
             ("admin",), member),
    ]
    run = {"lookup_plate": plate, "lookup_member": member}
    return tools, (lambda name, args: run[name](args) if name in run else {"error": "unknown tool"}), seen


def matcher(state: CaseState) -> dict:
    if "matcher" not in state["plan"]["agents"]:
        return {}
    pr = (state.get("findings") or {}).get("plate_reader") or {}
    read = [f"{p['text']} {p['state']} ({p['confidence']} confidence)".strip() for p in pr.get("plates") or []]
    r = state["report"]
    if not (read or r.get("plates") or r.get("names") or r.get("addresses")):
        return {"households": [], "findings": {"matcher": {"lookups": [], "matches": [], "unmatched": [],
                                                           "summary": "Nothing to look up: no plate, name or address in the report or photos."}},
                "steps": ["matcher"]}
    case = _report_text(state["report"]) + (f"\nPlates read from photos: {'; '.join(read)}" if read else "")
    tools, run_tool, seen = _match_tools(state)
    try:
        text, _, calls = models.generate_with_tools("case_match", _system("case_match"), [{"role": "user", "content": case}],
                                                    tools, run_tool, max_tokens=2048, temperature=0, max_rounds=MATCH_ROUNDS)
        j = models.parse_json(text)
    except Exception:
        log.warning("case matcher failed", exc_info=True)
        j, calls = {}, []
    # A match counts only if a tool actually returned that household (no invented matches).
    returned = {}
    for r in seen:
        for m in r.get("matches") or []:
            if m.get("household"):
                returned[(m["household"], m.get("address"))] = m
    households = []
    for m in j.get("matches") or []:
        if not isinstance(m, dict):
            continue
        hit = next((v for (h, a), v in returned.items() if h == m.get("household") and (not m.get("address") or a == m.get("address"))), None)
        if not hit:
            continue
        conf = m.get("confidence") if m.get("confidence") in ("high", "medium", "low") else "low"
        households.append({"name": hit["household"], "address": hit.get("address"), "status": hit.get("status"),
                           "confidence": conf, "basis": str(m.get("basis") or "")[:200]})
    households = _resolve_ids(households, bool(state.get("dry_run")))
    return {"households": households,
            "findings": {"matcher": {"lookups": [{"tool": c["tool"], "args": c["args"]} for c in calls],
                                     "claimed": len([m for m in j.get("matches") or [] if isinstance(m, dict)]),
                                     "summary": str(j.get("summary") or "")[:500],
                                     "unmatched": [str(x)[:100] for x in (j.get("unmatched") or [])][:10],
                                     "matches": [{k: h[k] for k in ("name", "address", "confidence", "basis")} for h in households]}},
            "steps": ["matcher"]}


def _resolve_ids(households: list[dict], dry_run: bool) -> list[dict]:
    from . import member_lookup
    if dry_run:
        ids = {(h["name"], h["address"]): h["id"] for h in member_lookup.FIXTURE}
        return [{**h, "id": ids.get((h["name"], h["address"]))} for h in households]
    out = []
    with tx() as conn:
        for h in households:
            r = conn.execute("select id from households where name = %s and address is not distinct from %s limit 1",
                             (h["name"], h["address"])).fetchone()
            out.append({**h, "id": r["id"] if r else None})
    return out


# ---------------- history ----------------

def history(state: CaseState) -> dict:
    if "history" not in state["plan"]["agents"]:
        return {}
    from .vehicles import norm_plate
    ids = [h["id"] for h in state.get("households") or [] if h.get("id") and h["confidence"] in ("high", "medium")]
    plates = [norm_plate(p) for p in state["report"].get("plates") or [] if len(norm_plate(p)) >= 5]
    plates += [norm_plate(p["text"]) for p in ((state.get("findings") or {}).get("plate_reader") or {}).get("plates") or []
               if "?" not in p["text"] and len(norm_plate(p["text"])) >= 5]
    if state.get("dry_run") or (not ids and not plates):
        return {"findings": {"history": {"earlier": []}}, "steps": ["history"]}
    with tx() as conn:
        rows = conn.execute(
            """select distinct t.id, t.title, t.status, t.created_at from incident_cases c join tickets t on t.id = c.ticket_id
               where c.ticket_id <> %(tid)s and (c.household_ids && %(ids)s::bigint[]
                 or exists (select 1 from unnest(%(plates)s::text[]) p
                            where regexp_replace(upper(c.report::text), '[^A-Z0-9]', '', 'g') like '%%' || p || '%%'))
               order by t.created_at desc limit 10""",
            {"tid": state["ticket_id"], "ids": ids, "plates": plates}).fetchall()
    return {"findings": {"history": {"earlier": [{"ticket_id": r["id"], "title": r["title"], "status": r["status"],
                                                  "at": r["created_at"].isoformat()} for r in rows]}},
            "steps": ["history"]}


# ---------------- policy (retrieval) ----------------

def policy(state: CaseState) -> dict:
    if "policy" not in state["plan"]["agents"]:
        return {}
    return {"findings": {"policy": policy_finding(state["report"])}, "steps": ["policy"]}


def policy_finding(r: dict, mode: str | None = None) -> dict:
    """Which rules apply and the process the board must follow. mode: "linear" (one pass) or "tot" (tree of
    thought, policy_tot.py); defaults to the admin setting cases.policy_reasoning."""
    from .retrieval import search
    mode = mode or settings().get("policy_reasoning") or "linear"
    query = " ".join(x for x in (r.get("what"), r.get("where"), "rules violation process notice hearing fine") if x)
    try:
        passages, _ = search(query[:500], "board", k=6)
    except Exception:
        log.warning("case policy search failed", exc_info=True)
        passages = []
    out = {"rules": [], "process": "", "confidence": "low", "sources": [], "mode": mode}
    if passages:
        try:
            if mode == "tot":
                from . import policy_tot
                out.update(policy_tot.run(_report_text(r), passages))
            else:
                out.update(_policy_linear(r, passages))
        except Exception:
            log.warning("case policy (%s) failed", mode, exc_info=True)
        out["sources"] = [{"n": i, "title": p.title, "section": p.section, "uri": p.uri} for i, p in enumerate(passages, 1)]
    return out


def _policy_linear(r: dict, passages: list) -> dict:
    numbered = "\n\n".join(f"[{i}] {p.title} — {p.section}\n{p.content[:1500]}" for i, p in enumerate(passages, 1))
    text, _ = models.generate("case_policy", _system("case_policy"), [{"role": "user", "content":
                              f"Incident:\n{_report_text(r)}\n\nPassages:\n{numbered}"}], json_mode=True,
                              max_tokens=2048, temperature=0)
    j = models.parse_json(text)
    return {"rules": [{"rule": str(x.get("rule") or "")[:300], "source": x.get("source"), "why": str(x.get("why") or "")[:300]}
                      for x in j.get("rules") or [] if isinstance(x, dict)][:4],
            "readings": [x for x in j.get("readings") or [] if isinstance(x, dict)][:3],
            "process": str(j.get("process") or "")[:600],
            "confidence": j.get("confidence") if j.get("confidence") in ("high", "medium", "low") else "low"}


# ---------------- writer ----------------

def _contact_block(households: list[dict], dry_run: bool) -> list[dict]:
    """Contact details and vehicles for matched households: added by code, never by the model."""
    from . import member_lookup, vehicles
    out = []
    for h in households:
        if h["confidence"] == "low" or not h.get("id"):
            continue
        if dry_run:
            f = next((x for x in member_lookup.FIXTURE if x["id"] == h["id"]), {})
            cars = [v for v in member_lookup.FIXTURE_VEHICLES if v["household_id"] == h["id"]]
            out.append({**h, "people": f.get("people", []), "emails": [], "phones": [],
                        "vehicles": [{"plate": v["plate"], "car": vehicles.describe(v)} for v in cars]})
            continue
        with tx() as conn:
            row = conn.execute("select phones from households where id = %s", (h["id"],)).fetchone()
            emails = conn.execute("select email, name from member_emails where household_id = %s order by id", (h["id"],)).fetchall()
            cars = conn.execute("select * from vehicles where household_id = %s order by plate_norm", (h["id"],)).fetchall()
        out.append({**h, "people": [e["name"] for e in emails if e["name"]], "emails": [e["email"] for e in emails],
                    "phones": list(row["phones"] or []) if row else [],
                    "vehicles": [{"plate": v["plate"], "state": v["state"], "car": vehicles.describe(v), "tag": v["tag"]} for v in cars]})
    return out


def brief(state: CaseState) -> dict:
    """What the writer gets: the findings, without contact details."""
    f = state.get("findings") or {}
    hh = state.get("households") or []
    return {
        "photos": {k: f.get("plate_reader", {}).get(k) for k in ("plates", "vehicles", "notes")} if "plate_reader" in f else None,
        "matches": [{k: h[k] for k in ("name", "address", "status", "confidence", "basis")} for h in hh],
        "matcher_summary": f.get("matcher", {}).get("summary"),
        "earlier_incidents": len(f.get("history", {}).get("earlier") or []),
        "rules": f.get("policy", {}).get("rules"), "process": f.get("policy", {}).get("process"),
    }


def write_model(report: dict, b: dict) -> dict:
    """The writer model's own output, before the code's guardrails (evals score this)."""
    text, _ = models.generate("case_writer", _system("case_writer"), [{"role": "user", "content":
                              f"Report:\n{_report_text(report)}\n\nFindings:\n{json.dumps(b, default=str)}"}],
                              json_mode=True, max_tokens=4096, temperature=0.2)
    return models.parse_json(text)


def writer(state: CaseState) -> dict:
    hh = state.get("households") or []
    j = {}
    try:
        j = write_model(state["report"], brief(state))
    except Exception:
        log.warning("case writer failed", exc_info=True)
    letter = j.get("letter") if isinstance(j.get("letter"), dict) and settings()["draft_letters"] else None
    if letter and not any(h["confidence"] in ("high", "medium") for h in hh):
        letter = None          # never a letter on a weak match, whatever the model said
    if letter:
        letter = {"subject": str(letter.get("subject") or "")[:200], "body": str(letter.get("body") or "")[:5000]}
    profile = {"summary": str(j.get("summary") or "The case file couldn't be summarized; see the findings.")[:1500],
               "assessment": str(j.get("assessment") or "")[:800], "next_step": str(j.get("next_step") or "")[:400],
               "households": _contact_block(hh, bool(state.get("dry_run")))}
    return {"profile": profile, "letter": letter, "steps": ["writer"]}


# ---------------- board decision (human in the loop) ----------------

def board(state: CaseState) -> dict:
    decision = interrupt({"case_id": state.get("case_id"), "awaiting": "board"})
    return {"decision": decision, "steps": ["board"]}


def finalize(state: CaseState) -> dict:
    d = state.get("decision") or {}
    if state.get("dry_run") or not state.get("case_id"):
        return {"steps": ["finalize"]}
    from . import tickets
    if d.get("by") == "board vote":
        note = d.get("note") or "The board voted on the case file."
    else:
        note = {"approve": "The board closed out the case file", "dismiss": "The board dismissed the case file"}.get(
            d.get("action"), "The board reviewed the case file")
        note += f": {d['note']}." if d.get("note") else "."
    try:
        tickets.comment(state["ticket_id"], note, role="admin", actor=d.get("by") or ACTOR)
    except Exception:
        log.warning("couldn't note the board decision on ticket %s", state["ticket_id"], exc_info=True)
    return {"steps": ["finalize"]}


def build(checkpointer=None):
    g = StateGraph(CaseState)
    for name, fn in [("supervisor", supervisor), ("plate_reader", plate_reader), ("matcher", matcher),
                     ("history", history), ("policy", policy), ("writer", writer), ("board", board), ("finalize", finalize)]:
        g.add_node(name, _span(name, fn))
    g.add_edge(START, "supervisor")
    g.add_edge("supervisor", "plate_reader")
    g.add_edge("plate_reader", "matcher")          # matcher and policy run in parallel
    g.add_edge("plate_reader", "policy")
    g.add_edge("matcher", "history")
    g.add_edge(["history", "policy"], "writer")    # writer waits for both branches
    g.add_edge("writer", "board")
    g.add_edge("board", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def _span(name, fn):
    import functools

    @functools.wraps(fn)
    def inner(state):
        with tracing.span(f"agent {name}", **{"langgraph.node": name, "cypress.case_id": state.get("case_id")}):
            return fn(state)
    return inner


_graph = None
_pool = None


def graph():
    global _graph, _pool
    if _graph is None:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        from .config import settings as cfg
        _pool = ConnectionPool(cfg.database_url, min_size=1, max_size=2, open=True,
                               kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
        saver = PostgresSaver(_pool)
        saver.setup()
        _graph = build(saver)
    return _graph


def _config(case_id: int) -> dict:
    return {"configurable": {"thread_id": f"case:{case_id}"}}


# ---------------- queue, run, decide ----------------

def open_case(ticket_id: int, report: dict, attachments: list[str], created_by: str) -> int:
    with tx() as conn:
        cid = conn.execute("""insert into incident_cases (ticket_id, report, attachments, created_by)
                              values (%s, %s, %s, %s) returning id""",
                           (ticket_id, Jsonb(report), attachments, created_by)).fetchone()["id"]
    from .ingest.trigger import start_ingest
    start_ingest()
    return cid


def claim() -> dict | None:
    with tx() as conn:
        return conn.execute(
            """update incident_cases set status = 'running', started_at = now()
               where id = (select id from incident_cases where status = 'queued' order by created_at
                           for update skip locked limit 1) returning *""").fetchone()


def run(case: dict) -> None:
    """Build the case file up to the board's decision. Runs in the worker."""
    tid, _, _ = tracing.current_ids()
    state = {"case_id": case["id"], "ticket_id": case["ticket_id"], "report": case["report"],
             "attachments": list(case["attachments"] or []), "findings": {}, "steps": []}
    try:
        graph().invoke(state, _config(case["id"]))
        v = graph().get_state(_config(case["id"])).values
        hh = [h["id"] for h in v.get("households") or [] if h.get("id") and h["confidence"] in ("high", "medium")]
        with tx() as conn:
            conn.execute("""update incident_cases set status = 'awaiting_board', plan = %s, findings = %s, household_ids = %s,
                              profile = %s, letter = %s, trace_id = %s, finished_at = now() where id = %s""",
                         (Jsonb(v.get("plan")), Jsonb(v.get("findings") or {}), hh, Jsonb(v.get("profile")),
                          Jsonb(v.get("letter")) if v.get("letter") else None, tid, case["id"]))
        _announce(case, matched=bool(hh), letter=bool(v.get("letter")))
        if v.get("letter"):
            from . import votes
            votes.check_after_open(case["id"])
    except Exception as e:
        log.exception("case %s failed", case["id"])
        with tx() as conn:
            conn.execute("update incident_cases set status = 'failed', error = %s, finished_at = now() where id = %s",
                         (f"{type(e).__name__}: {e}"[:300], case["id"]))


def _announce(case: dict, *, matched: bool, letter: bool) -> None:
    """A note on the ticket everyone with the ticket can see: no names, addresses or contact details."""
    from . import tickets
    # Ticket history is visible to staff too, so it says nothing about whether a member household matched.
    msg = "Case file ready for the board" + (", with a draft notice letter waiting for a vote." if letter else ".")
    try:
        tickets.comment(case["ticket_id"], msg, role="admin", actor=ACTOR)
    except Exception:
        log.warning("couldn't note the case file on ticket %s", case["ticket_id"], exc_info=True)


class CaseError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def resolve(c: dict, action: str, note: str | None, *, by: str, letter: dict | None = None, user=None) -> None:
    """Resume the paused graph with the board's decision and record it (used by decide() and by votes)."""
    from .audit import audit
    if action == "approve" and letter is None:
        letter = c["letter"]
    decision = {"action": action, "note": (note or "").strip()[:500] or None, "letter": letter if action == "approve" else None,
                "by": by}
    graph().invoke(Command(resume=decision), _config(c["id"]))
    with tx() as conn:
        conn.execute("""update incident_cases set status = %s, decision = %s, letter = coalesce(%s, letter),
                          decided_by = %s, decided_at = now() where id = %s""",
                     ("approved" if action == "approve" else "dismissed", Jsonb(decision),
                      Jsonb(letter) if letter and action == "approve" else None, by, c["id"]))
        if user is not None:
            audit(conn, user, f"case_{action}", "ticket", c["ticket_id"], {"letter": bool(decision["letter"])})


def _awaiting(ticket_id: int) -> dict:
    with tx() as conn:
        c = conn.execute("select * from incident_cases where ticket_id = %s", (ticket_id,)).fetchone()
    if not c:
        raise CaseError(404, "This ticket has no case file")
    if c["status"] != "awaiting_board":
        raise CaseError(409, f"The case file is {c['status'].replace('_', ' ')}, not waiting for the board")
    return c


def decide(ticket_id: int, action: str, note: str | None, letter: dict | None, user) -> dict:
    """One board member's decision. A case with a notice letter can only be approved by a board vote
    (votes.py); without a letter (nothing to send) a board member can close it out directly."""
    c = _awaiting(ticket_id)
    if c["vote_status"] == "open":
        raise CaseError(409, "A board vote is open on this case; it closes when the vote does")
    if action == "approve" and (letter or c["letter"]):
        raise CaseError(409, "Notice letters need a board vote. Start the vote instead.")
    resolve(c, action, note, by=user.email, user=user)
    return view(ticket_id, user)


def edit_letter(ticket_id: int, letter: dict, user) -> dict:
    """Edit the draft before a vote opens (an open vote would be voting on different words)."""
    from .audit import audit
    c = _awaiting(ticket_id)
    if c["vote_status"] == "open":
        raise CaseError(409, "Cancel the open vote before editing the letter")
    with tx() as conn:
        conn.execute("update incident_cases set letter = %s where id = %s", (Jsonb(letter), c["id"]))
        audit(conn, user, "case_letter_edit", "ticket", ticket_id)
    return view(ticket_id, user)


def view(ticket_id: int, user) -> dict | None:
    """The case file as this user may see it."""
    from . import votes
    with tx() as conn:
        c = conn.execute("select * from incident_cases where ticket_id = %s", (ticket_id,)).fetchone()
    if not c:
        return None
    if c["vote_status"] == "open" and votes.expire_if_due(c):
        return view(ticket_id, user)
    from .roles import MEMBER_DATA_ROLES
    full = (user.role in settings()["full_roles"] and user.role in MEMBER_DATA_ROLES) or user.role == "admin"
    from .roles import can_lookup_members
    base = {"status": c["status"], "full": full, "created_at": c["created_at"],
            "finished_at": c["finished_at"], "decided_at": c["decided_at"]}
    if can_lookup_members(user.role):
        base["matched"] = bool(c["household_ids"])
    if not full:
        return base
    from .tracing import console_url
    return {**base, "report": c["report"], "plan": c["plan"], "findings": c["findings"], "profile": c["profile"],
            "letter": c["letter"], "decision": c["decision"], "decided_by": c["decided_by"], "error": c["error"],
            "vote": votes.view(c, user.email), "vote_rule": votes.board_ready(), "is_admin": user.role == "admin",
            "trace_url": console_url(c["trace_id"]) if user.role == "admin" else None}


def run_dry(report: dict, attachments: list[str] | None = None) -> dict:
    """Run the graph to the board step against the fictional directory, without a database case (evals, tests)."""
    from langgraph.checkpoint.memory import InMemorySaver
    g = build(InMemorySaver())
    cfg = {"configurable": {"thread_id": "dry"}}
    g.invoke({"case_id": 0, "ticket_id": 0, "report": report, "attachments": attachments or [], "dry_run": True,
              "findings": {}, "steps": []}, cfg)
    return g.get_state(cfg).values
