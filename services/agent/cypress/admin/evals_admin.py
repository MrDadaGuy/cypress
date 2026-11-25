"""Admin: eval test set and runs. Admin-only; runs are metered like any traffic (channel 'eval')."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import evals, models, subagent_evals
from ..audit import audit
from ..auth import User, require, require_internal
from ..db import tx

router = APIRouter(tags=["admin: evals"])
admin = require("admin")


class Expect(BaseModel):
    intent: str | None = None
    must_include: list[list[str]] = Field(default_factory=list, max_length=10)
    must_not_include: list[str] = Field(default_factory=list, max_length=20)
    cite: list[str] = Field(default_factory=list, max_length=10)
    tool: str | None = None
    ideal: str | None = Field(default=None, max_length=2000)


class CaseIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    as_role: Literal["public", "member", "staff", "security", "board"] = "public"
    expect: Expect = Field(default_factory=Expect)
    tags: list[str] = Field(default_factory=list, max_length=10)
    notes: str | None = Field(default=None, max_length=1000)
    active: bool = True


class RunIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    prompts: dict[str, str] = Field(default_factory=dict)       # draft prompt per key
    models: dict[str, str] = Field(default_factory=dict)        # model per step
    case_ids: list[int] | None = None
    tag: str | None = None


def _expect(e: Expect) -> dict:
    return {k: v for k, v in e.model_dump().items() if v not in (None, [], "")}


@router.get("/api/admin/evals")
def overview(user: User = Depends(admin)):
    seeded = evals.seed_if_empty(user.email)
    with tx() as conn:
        cases = conn.execute("""select c.*, (select passed from eval_results r join eval_runs u on u.id = r.run_id
                                              where r.case_id = c.id and r.status <> 'pending'
                                              order by u.started_at desc limit 1) as last_passed
                                from eval_cases c order by c.id""").fetchall()
        runs = conn.execute("select * from eval_runs order by started_at desc limit 25").fetchall()
    return {"cases": cases, "runs": [{**r, **evals.summary(r["id"])} for r in runs], "seeded": seeded,
            "tags": sorted({t for c in cases for t in c["tags"]})}


@router.post("/api/admin/evals/cases", status_code=201)
def add_case(body: CaseIn, user: User = Depends(admin)):
    with tx() as conn:
        cid = conn.execute("""insert into eval_cases (question, as_role, expect, tags, notes, active, created_by)
                              values (%s, %s, %s, %s, %s, %s, %s) returning id""",
                           (body.question, body.as_role, Jsonb(_expect(body.expect)), body.tags, body.notes, body.active,
                            user.email)).fetchone()["id"]
        audit(conn, user, "create", "eval_case", cid)
    return {"id": cid}


@router.put("/api/admin/evals/cases/{cid}")
def save_case(cid: int, body: CaseIn, user: User = Depends(admin)):
    with tx() as conn:
        if not conn.execute("""update eval_cases set question = %s, as_role = %s, expect = %s, tags = %s, notes = %s,
                                 active = %s, updated_at = now() where id = %s returning id""",
                            (body.question, body.as_role, Jsonb(_expect(body.expect)), body.tags, body.notes, body.active,
                             cid)).fetchone():
            raise HTTPException(404, "Not found")
        audit(conn, user, "update", "eval_case", cid)
    return {"id": cid}


@router.delete("/api/admin/evals/cases/{cid}", status_code=204)
def delete_case(cid: int, user: User = Depends(admin)):
    with tx() as conn:
        if not conn.execute("delete from eval_cases where id = %s returning id", (cid,)).fetchone():
            raise HTTPException(404, "Not found")
        audit(conn, user, "delete", "eval_case", cid)


@router.post("/api/admin/evals/runs", status_code=201)
def start_run(body: RunIn, user: User = Depends(admin)):
    from ..agent import prompts, service
    for key, text in body.prompts.items():
        if key not in prompts.PROMPTS:
            raise HTTPException(422, f"Unknown prompt {key}")
        if problems := prompts.check(key, text):
            raise HTTPException(422, f"{key}: {' '.join(problems)}")
    for node, mid in body.models.items():
        m = models.catalog().get(mid)
        if node not in models.NODES or not m or m.kind != "chat" or not m.enabled:
            raise HTTPException(422, f"{node}: {mid} isn't an enabled chat model")
        if node in models.GEMINI_ONLY and m.provider not in models.TOOL_PROVIDERS:
            raise HTTPException(422, f"The {node} step needs a Gemini model")
    try:
        service.check_rate(f"eval_run:{user.email}", "eval_run")
    except service.RateLimited:
        raise HTTPException(429, "Too many eval runs for now; try again later.") from None
    ids = body.case_ids
    if body.tag and not ids:
        with tx() as conn:
            ids = [r["id"] for r in conn.execute("select id from eval_cases where active and %s = any(tags)", (body.tag,))]
    try:
        rid = evals.start(body.label, {"prompts": body.prompts, "models": body.models}, user.email, ids)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    with tx() as conn:
        audit(conn, user, "start", "eval_run", rid, {"label": body.label, "prompts": list(body.prompts), "models": body.models})
    return {"id": rid}


@router.get("/api/admin/evals/runs/{rid}")
def get_run(rid: int, _: User = Depends(admin)):
    with tx() as conn:
        run = conn.execute("select * from eval_runs where id = %s", (rid,)).fetchone()
        if not run:
            raise HTTPException(404, "Not found")
        results = conn.execute("""select r.*, c.question, c.as_role, c.tags, c.expect
                                  from eval_results r join eval_cases c on c.id = r.case_id
                                  where r.run_id = %s order by r.passed nulls first, c.id""", (rid,)).fetchall()
    return {**run, **evals.summary(rid), "results": results}


@router.post("/api/admin/evals/runs/{rid}/cancel")
def cancel_run(rid: int, user: User = Depends(admin)):
    with tx() as conn:
        conn.execute("update eval_runs set status = 'cancelled', finished_at = now() where id = %s and status = 'running'", (rid,))
        audit(conn, user, "cancel", "eval_run", rid)
    return {"ok": True}


@router.post("/api/evals/work")
async def work(request: Request):
    """Cloud Tasks: run one test question for a run."""
    require_internal(request)
    body = await request.json()
    if body.get("kind") == "subagent":
        await run_in_threadpool(subagent_evals.run_case, int(body["run_id"]), str(body["case_key"]), str(body["model"]))
    else:
        await run_in_threadpool(evals.run_case, int(body["run_id"]), int(body["case_id"]))
    return {"ok": True}


# ---------------- sub-agent evals (case file) ----------------

class SubRunIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    nodes: list[str] = Field(min_length=1, max_length=10)
    models: list[str] = Field(min_length=1, max_length=6)       # model ids, or "current" for each step's live model
    prompts: dict[str, str] = Field(default_factory=dict)


class Rubric(BaseModel):
    rubric: str = Field(default="", max_length=6000)
    pass_score: int = Field(default=subagent_evals.DEFAULT_PASS, ge=1, le=5)


@router.get("/api/admin/evals/subagents")
def subagents(_: User = Depends(admin)):
    with tx() as conn:
        runs = conn.execute("select * from subagent_eval_runs order by started_at desc limit 30").fetchall()
    return {"nodes": [{"node": n, "about": a, "cases": sum(1 for c in subagent_evals.CASES if c["node"] == n),
                       "gemini_only": n in models.GEMINI_ONLY, "current": models.model_for(subagent_evals.model_node(n)).id}
                      for n, a in subagent_evals.NODES.items()],
            "cases": [{k: c[k] for k in ("key", "node", "title", "expect")} for c in subagent_evals.CASES],
            "rubrics": subagent_evals.rubrics(),
            "runs": [{**r, **subagent_evals.summary(r["id"])} for r in runs]}


@router.put("/api/admin/evals/subagents/rubrics/{node}")
def put_rubric(node: str, body: Rubric, user: User = Depends(admin)):
    from .. import org
    if node not in subagent_evals.NODES:
        raise HTTPException(404, "Unknown sub-agent")
    r = {**subagent_evals.rubrics(), node: body.model_dump()}
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('subagent_rubrics', %s, %s)
                        on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by,
                          updated_at = now()""", (Jsonb(r), user.email))
        audit(conn, user, "update", "subagent_rubric", node, {"chars": len(body.rubric), "pass_score": body.pass_score})
    org.clear()
    return r


@router.post("/api/admin/evals/subagents/runs", status_code=201)
def start_subagent_run(body: SubRunIn, user: User = Depends(admin)):
    from ..agent import prompts, service
    if bad := [n for n in body.nodes if n not in subagent_evals.NODES]:
        raise HTTPException(422, f"Unknown sub-agent: {', '.join(bad)}")
    for mid in body.models:
        m = models.catalog().get(mid)
        if mid != "current" and (not m or m.kind != "chat" or not m.enabled):
            raise HTTPException(422, f"{mid} isn't an enabled chat model")
    for key, text in body.prompts.items():
        if key not in body.nodes or key not in prompts.PROMPTS:
            raise HTTPException(422, f"A draft prompt is only for a sub-agent in this run ({key})")
        if problems := prompts.check(key, text):
            raise HTTPException(422, f"{key}: {' '.join(problems)}")
    try:
        service.check_rate(f"eval_run:{user.email}", "eval_run")
    except service.RateLimited:
        raise HTTPException(429, "Too many eval runs for now; try again later.") from None
    try:
        rid = subagent_evals.start(body.label, body.nodes, list(dict.fromkeys(body.models)), user.email, body.prompts)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    with tx() as conn:
        audit(conn, user, "start", "subagent_eval_run", rid, {"label": body.label, "nodes": body.nodes, "models": body.models})
    return {"id": rid}


@router.get("/api/admin/evals/subagents/runs/{rid}")
def get_subagent_run(rid: int, _: User = Depends(admin)):
    with tx() as conn:
        run = conn.execute("select * from subagent_eval_runs where id = %s", (rid,)).fetchone()
        if not run:
            raise HTTPException(404, "Not found")
        results = conn.execute("""select * from subagent_eval_results where run_id = %s
                                  order by node, case_key, model""", (rid,)).fetchall()
    titles = {c["key"]: c["title"] for c in subagent_evals.CASES}
    return {**run, **subagent_evals.summary(rid), "scoreboard": subagent_evals.scoreboard(rid),
            "results": [{**r, "title": titles.get(r["case_key"], r["case_key"])} for r in results]}


@router.post("/api/admin/evals/subagents/runs/{rid}/cancel")
def cancel_subagent_run(rid: int, user: User = Depends(admin)):
    with tx() as conn:
        conn.execute("update subagent_eval_runs set status = 'cancelled', finished_at = now() where id = %s and status = 'running'", (rid,))
        audit(conn, user, "cancel", "subagent_eval_run", rid)
    return {"ok": True}
