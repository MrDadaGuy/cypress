"""Admin: model catalog, node assignments, runtime knobs, budget, and LLM spend (FinOps).

Everything here is admin-only, including spend figures. Principles: nothing hard-coded that an
admin might want to change, and no unmetered spend (a model can't be switched on without a price).
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from .. import finops, models, org
from ..audit import audit
from ..auth import User, require
from ..db import tx

router = APIRouter(prefix="/api/admin", tags=["admin: models & spend"])
admin = require("admin")

Price = Field(default=None, ge=0, le=1000)


class ModelIn(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,48}$")
    kind: Literal["chat", "embed", "rerank"] = "chat"
    provider: Literal["gemini", "claude", "maas", "vertex_search", "local"]
    name: str = Field(min_length=2, max_length=120)
    region: str = Field(default="global", pattern=r"^[a-z0-9-]{2,30}$")
    label: str = Field(min_length=2, max_length=80)
    thinking: Literal["minimal", "low", "medium", "high"] | None = None
    input_per_m: float | None = Price
    cached_per_m: float | None = Price
    output_per_m: float | None = Price
    per_call_usd: float | None = Field(default=None, ge=0, le=10)
    price_source: str | None = Field(default=None, max_length=200)
    enabled: bool = False
    note: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def check(self):
        if self.kind == "rerank" and self.provider not in ("vertex_search", "local"):
            raise ValueError("Rerankers use the vertex_search or local provider")
        if self.kind != "rerank" and self.provider in ("vertex_search", "local"):
            raise ValueError(f"{self.provider} is only for rerankers")
        if self.kind == "embed" and self.provider != "gemini":
            raise ValueError("Embeddings are wired for Gemini only")
        if self.enabled and not models.Model(**self.model_dump()).priced:
            raise ValueError("Enter prices before switching a model on, so its spend is metered")
        return self


class ConfigIn(BaseModel):
    node: str
    audience: Literal["all", "residents", "team"] = "all"
    model_id: str | None = None              # null on residents/team = same as 'all'
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=16, le=32768)
    note: str | None = Field(default=None, max_length=300)


class RestoreIn(BaseModel):
    note: str | None = Field(default=None, max_length=300)


class RuntimeIn(BaseModel):
    fallback_model: str
    hedge_s: float = Field(ge=0.5, le=30)
    stream_hedge_s: float = Field(ge=0.5, le=30)
    timeout_s: int = Field(ge=5, le=120)


class BudgetIn(BaseModel):
    monthly_usd: float = Field(ge=0, le=10_000)
    guard: bool
    guard_model: str
    alert_pct: int = Field(ge=1, le=100)


class SettingsIn(BaseModel):
    runtime: RuntimeIn
    budget: BudgetIn


def _chat_model(mid: str, what: str) -> models.Model:
    m = models.catalog().get(mid)
    if not m or m.kind != "chat":
        raise HTTPException(422, f"{what}: unknown chat model {mid}")
    if not m.enabled:
        raise HTTPException(422, f"{what}: {m.label} is switched off")
    return m


def _budget_status() -> dict:
    b = finops.budget()
    finops.clear()
    mtd = finops.month_to_date()
    with tx() as conn:
        d = conn.execute(
            """select extract(day from now() at time zone 'America/Chicago')::int as day,
                      extract(day from (date_trunc('month', now() at time zone 'America/Chicago')
                        + interval '1 month - 1 day'))::int as days""").fetchone()
    projected = mtd / max(d["day"], 1) * d["days"]
    limit = float(b.get("monthly_usd") or 0)
    return {**b, "month_to_date": round(mtd, 4), "projected": round(projected, 4), "day": d["day"],
            "days_in_month": d["days"], "guard_active": bool(finops.guard_model()),
            "alert": bool(limit) and mtd >= limit * b.get("alert_pct", 80) / 100}


@router.get("/models")
def get_models(_: User = Depends(admin)):
    models.clear_cache()
    org.clear()
    cat = models.catalog()
    with tx() as conn:
        rows = conn.execute("select * from llm_models order by kind, provider, label").fetchall()
        stats = {r["model_id"]: r for r in conn.execute(
            """select model_id, count(*) as calls, coalesce(sum(cost_usd), 0)::float as cost,
                      count(*) filter (where cost_usd is null) as unpriced, max(at) as last_used
               from llm_calls where at > now() - interval '30 days' group by model_id""")}
    live = models.live_configs()

    def effective(n: str, a: str) -> str:
        return (models.gemini_for(n, a) if n in models.GEMINI_ONLY else models.model_for(n, a)).id

    nodes = [{"node": n, "description": desc, "default": d, "gemini_only": n in models.GEMINI_ONLY,
              "configs": {a: _cfg_out(live.get((n, a))) for a in models.AUDIENCES},
              "resolved": {a: models.config(n, a) for a in models.AUDIENCES},
              "effective": {a: effective(n, a) for a in models.AUDIENCES}}
             for n, (desc, d) in models.NODES.items()]
    out = []
    for r in rows:
        s = stats.get(r["id"]) or {}
        out.append({**{k: (float(v) if v is not None and (k.endswith("_per_m") or k == "per_call_usd") else v)
                       for k, v in r.items()},
                    "priced": cat[r["id"]].priced if r["id"] in cat else False,
                    "calls_30d": s.get("calls", 0), "cost_30d": round(s.get("cost", 0.0), 4),
                    "unpriced_30d": s.get("unpriced", 0), "last_used": s.get("last_used"),
                    "assigned_to": sorted({f"{n['node']} ({a})" if a != "all" else n["node"]
                                           for n in nodes for a in models.AUDIENCES
                                           if (n["configs"][a] or {}).get("model_id") == r["id"]})})
    return {"nodes": nodes, "models": out, "runtime": models.runtime(), "budget": _budget_status()}


def _probe_rate(user: User) -> None:
    from ..agent import service
    try:
        service.check_rate(f"probe:{user.email}", "probe")
    except service.RateLimited:
        raise HTTPException(429, "Too many test calls for now; try again in a while.") from None


def _cfg_out(c: dict | None) -> dict | None:
    if not c:
        return None
    return {**c, "temperature": float(c["temperature"]) if c.get("temperature") is not None else None}


def _new_version(conn, user: User, node: str, audience: str, fields: dict, note: str | None) -> dict:
    v = conn.execute("""select coalesce(max(version), 0) + 1 as v from agent_configs where node = %s and audience = %s""",
                     (node, audience)).fetchone()["v"]
    row = conn.execute(
        """insert into agent_configs (node, audience, version, model_id, temperature, max_tokens, prompt, note, created_by)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning *""",
        (node, audience, v, fields.get("model_id"), fields.get("temperature"), fields.get("max_tokens"),
         fields.get("prompt"), note, user.email)).fetchone()
    audit(conn, user, "new_version", "agent_config", f"{node}/{audience}",
          {"version": v, **{k: fields.get(k) for k in ("model_id", "temperature", "max_tokens")},
           "prompt_changed": fields.get("prompt") is not None, "note": note})
    return row


def _check_model_for(node: str, mid: str) -> None:
    m = _chat_model(mid, models.NODES[node][0])
    if node in models.GEMINI_ONLY and m.provider not in models.TOOL_PROVIDERS:
        raise HTTPException(422, f"The {node} step needs a Gemini model (tool calling / image input)")


@router.post("/agent-configs", status_code=201)
def save_config(body: ConfigIn, user: User = Depends(admin)):
    """Save a new version of a step's config. The prompt carries over from the live version."""
    if body.node not in models.NODES:
        raise HTTPException(404, "Unknown step")
    if body.model_id:
        _check_model_for(body.node, body.model_id)
    elif body.audience == "all":
        raise HTTPException(422, "Choose a model for the default (all) config")
    live = models.live_configs().get((body.node, body.audience)) or {}
    fields = {"model_id": body.model_id, "temperature": body.temperature, "max_tokens": body.max_tokens,
              "prompt": live.get("prompt")}
    with tx() as conn:
        row = _new_version(conn, user, body.node, body.audience, fields, body.note)
    models.clear_cache()
    return {"saved": _cfg_out(row), **get_models(user)}


@router.get("/agent-configs/{node}/{audience}")
def config_history(node: str, audience: str, _: User = Depends(admin)):
    """Every version of a step's config, newest first, with what each version cost while live."""
    finops.flush()
    with tx() as conn:
        rows = conn.execute(
            """select c.*, count(l.id) as calls, coalesce(sum(l.cost_usd), 0)::float as cost,
                      (percentile_cont(0.5) within group (order by l.latency_ms))::int as p50_ms
               from agent_configs c left join llm_calls l on l.config_id = c.id
               where c.node = %s and c.audience = %s group by c.id order by c.version desc""", (node, audience)).fetchall()
    return {"node": node, "audience": audience, "versions": [_cfg_out(r) for r in rows]}


@router.post("/agent-configs/{cid}/restore", status_code=201)
def restore_config(cid: int, body: RestoreIn, user: User = Depends(admin)):
    """Copy an old version forward as the new live version (history stays append-only)."""
    with tx() as conn:
        old = conn.execute("select * from agent_configs where id = %s", (cid,)).fetchone()
        if not old:
            raise HTTPException(404, "Unknown version")
        if old["model_id"] and old["node"] in models.NODES:
            _check_model_for(old["node"], old["model_id"])
        row = _new_version(conn, user, old["node"], old["audience"], old,
                           body.note or f"Restored version {old['version']}")
    models.clear_cache()
    return {"saved": _cfg_out(row), **get_models(user)}


COLS = ("kind", "provider", "name", "region", "label", "thinking", "input_per_m", "cached_per_m", "output_per_m",
        "per_call_usd", "price_source", "enabled", "note")


@router.post("/models", status_code=201)
def create_model(body: ModelIn, user: User = Depends(admin)):
    with tx() as conn:
        if conn.execute("select 1 from llm_models where id = %s", (body.id,)).fetchone():
            raise HTTPException(409, "A model with that id already exists")
        conn.execute(f"insert into llm_models (id, {', '.join(COLS)}, updated_by) values ({', '.join(['%s'] * (len(COLS) + 2))})",
                     (body.id, *[getattr(body, c) for c in COLS], user.email))
        if body.kind == "rerank" and body.enabled:
            conn.execute("update llm_models set enabled = false where kind = 'rerank' and id <> %s", (body.id,))
        audit(conn, user, "create", "llm_model", body.id, body.model_dump())
    models.clear_cache()
    return get_models(user)


def _in_use(mid: str) -> list[str]:
    rt, b = models.runtime(), finops.budget()
    uses = sorted({f"{n} ({a})" if a != "all" else n for (n, a), c in models.live_configs().items()
                   if c.get("model_id") == mid})
    if rt.get("fallback_model") == mid:
        uses.append("fallback model")
    if b.get("guard_model") == mid:
        uses.append("budget guard model")
    return uses


@router.put("/models/{mid}")
def update_model(mid: str, body: ModelIn, user: User = Depends(admin)):
    if body.id != mid:
        raise HTTPException(422, "Model ids can't be changed; add a new model instead")
    if not body.enabled and (uses := _in_use(mid)):
        raise HTTPException(409, f"In use as: {', '.join(uses)}. Reassign those first.")
    with tx() as conn:
        before = conn.execute("select * from llm_models where id = %s", (mid,)).fetchone()
        if not before:
            raise HTTPException(404, "Unknown model")
        conn.execute(f"update llm_models set {', '.join(c + ' = %s' for c in COLS)}, updated_by = %s, updated_at = now() "
                     "where id = %s", (*[getattr(body, c) for c in COLS], user.email, mid))
        if body.kind == "rerank" and body.enabled:
            # One reranker at a time: switching one on switches the others off.
            for r in conn.execute("""update llm_models set enabled = false, updated_by = %s, updated_at = now()
                                     where kind = 'rerank' and enabled and id <> %s returning id""", (user.email, mid)):
                audit(conn, user, "update", "llm_model", r["id"], {"enabled": False, "reason": f"{mid} switched on"})
        changed = {c: getattr(body, c) for c in COLS
                   if (float(before[c]) if isinstance(getattr(body, c), float) and before[c] is not None else before[c])
                   != getattr(body, c)}
        audit(conn, user, "update", "llm_model", mid, changed)
    models.clear_cache()
    return get_models(user)


@router.delete("/models/{mid}")
def delete_model(mid: str, user: User = Depends(admin)):
    if uses := _in_use(mid):
        raise HTTPException(409, f"In use as: {', '.join(uses)}. Reassign those first.")
    with tx() as conn:
        if conn.execute("select 1 from agent_configs where model_id = %s limit 1", (mid,)).fetchone():
            raise HTTPException(409, "Older config versions use this model, so it stays for history. Switch it off instead.")
        if not conn.execute("delete from llm_models where id = %s returning id", (mid,)).fetchone():
            raise HTTPException(404, "Unknown model")
        audit(conn, user, "delete", "llm_model", mid)
    models.clear_cache()
    return get_models(user)


@router.post("/models/{mid}/test")
def test_model(mid: str, user: User = Depends(admin)):
    _probe_rate(user)
    models.clear_cache()
    r = models.probe(mid)
    finops.flush()
    return r


@router.post("/models/{mid}/reprice")
def reprice(mid: str, user: User = Depends(admin)):
    """Apply this model's current price to its unpriced calls (recovers spend metered before a price existed)."""
    m = models.catalog().get(mid)
    if not m or not m.priced:
        raise HTTPException(422, "Give the model a price first")
    with tx() as conn:
        n = conn.execute(
            """update llm_calls set cost_usd =
                 ((input_tokens - least(cached_tokens, input_tokens)) * %(i)s + least(cached_tokens, input_tokens) * %(c)s
                  + output_tokens * %(o)s) / 1000000.0 + %(r)s
               where model_id = %(id)s and cost_usd is null""",
            {"i": m.input_per_m or 0, "c": m.cached_per_m if m.cached_per_m is not None else (m.input_per_m or 0),
             "o": m.output_per_m or 0, "r": m.per_call_usd or 0, "id": mid}).rowcount
        audit(conn, user, "reprice", "llm_model", mid, {"calls": n})
    finops.clear()
    return {"repriced": n}


@router.put("/llm-settings")
def put_settings(body: SettingsIn, user: User = Depends(admin)):
    _chat_model(body.runtime.fallback_model, "Fallback model")
    _chat_model(body.budget.guard_model, "Budget guard model")
    with tx() as conn:
        for key, value in (("llm_runtime", body.runtime.model_dump()), ("llm_budget", body.budget.model_dump())):
            conn.execute("""insert into org_settings (key, value, updated_by) values (%s, %s, %s)
                            on conflict (key) do update set value = excluded.value,
                              updated_by = excluded.updated_by, updated_at = now()""", (key, Jsonb(value), user.email))
        audit(conn, user, "update", "org_settings", "llm", body.model_dump())
    org.clear()
    models.clear_cache()
    return get_models(user)


# ---------------- spend ----------------

@router.get("/usage")
def usage(days: int = Query(30, ge=1, le=366), _: User = Depends(admin)):
    finops.flush()
    p = {"days": days}
    since = "at > now() - make_interval(days => %(days)s)"
    with tx() as conn:
        totals = conn.execute(f"""
            select count(*) as calls, coalesce(sum(cost_usd), 0)::float as cost,
                   count(distinct turn_id) as turns,
                   coalesce(sum(cost_usd) filter (where turn_id is not null), 0)::float as turn_cost,
                   coalesce(sum(input_tokens), 0)::bigint as input_tokens,
                   coalesce(sum(output_tokens), 0)::bigint as output_tokens,
                   count(*) filter (where cost_usd is null) as unpriced,
                   count(*) filter (where estimated) as estimated,
                   count(*) filter (where not ok) as failed,
                   coalesce(sum(cost_usd) filter (where purpose = 'hedge'), 0)::float as hedge_cost,
                   coalesce(sum(cost_usd) filter (where purpose = 'fallback'), 0)::float as fallback_cost,
                   coalesce(sum(cost_usd) filter (where purpose = 'probe'), 0)::float as probe_cost
            from llm_calls where {since}""", p).fetchone()
        daily = conn.execute(f"""
            select to_char(date_trunc('day', at at time zone 'America/Chicago'), 'YYYY-MM-DD') as day, model_id,
                   count(*) as calls, coalesce(sum(cost_usd), 0)::float as cost
            from llm_calls where {since} group by 1, 2 order by 1, 2""", p).fetchall()
        by_node = conn.execute(f"""
            select node, model_id, count(*) as calls, coalesce(sum(cost_usd), 0)::float as cost,
                   coalesce(sum(input_tokens), 0)::bigint as input_tokens,
                   coalesce(sum(cached_tokens), 0)::bigint as cached_tokens,
                   coalesce(sum(output_tokens), 0)::bigint as output_tokens,
                   (percentile_cont(0.5) within group (order by latency_ms))::int as p50_ms,
                   (percentile_cont(0.95) within group (order by latency_ms))::int as p95_ms,
                   count(*) filter (where not ok) as failed,
                   count(*) filter (where purpose in ('hedge', 'fallback')) as backups
            from llm_calls where {since} group by 1, 2 order by cost desc""", p).fetchall()
        by_channel = conn.execute(f"""
            select coalesce(channel, 'background') as channel, coalesce(user_role, 'resident') as role,
                   count(distinct turn_id) as turns, coalesce(sum(cost_usd), 0)::float as cost
            from llm_calls where {since} group by 1, 2 order by cost desc""", p).fetchall()
        top_turns = conn.execute(f"""
            select c.turn_id, min(c.at) as at, max(c.channel) as channel, count(*) as calls,
                   coalesce(sum(c.cost_usd), 0)::float as cost, max(i.intent) as intent, max(i.id) as interaction_id
            from llm_calls c left join interactions i on i.public_id = c.turn_id
            where c.{since} and c.turn_id is not null group by c.turn_id order by cost desc limit 10""", p).fetchall()
    # What would each chat step have cost on another model? Same tokens, other prices.
    chat = [m for m in models.catalog().values() if m.kind == "chat" and m.priced]
    agg: dict[str, dict] = {}
    for r in by_node:
        if r["node"] not in models.NODES:
            continue
        a = agg.setdefault(r["node"], {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "cost": 0.0})
        for k in ("input_tokens", "cached_tokens", "output_tokens"):
            a[k] += r[k]
        a["cost"] += r["cost"]
    what_if = [{"node": n, "actual": round(a["cost"], 4), "tokens": {k: a[k] for k in ("input_tokens", "output_tokens")},
                "options": sorted(({"model_id": m.id, "label": m.label, "enabled": m.enabled,
                                    "gemini_ok": n not in models.GEMINI_ONLY or m.provider in models.TOOL_PROVIDERS,
                                    "cost": round(finops.cost(m, finops.Usage(a["input_tokens"], a["cached_tokens"],
                                                                              a["output_tokens"], requests=0)) or 0, 4)}
                                   for m in chat), key=lambda o: o["cost"])}
               for n, a in agg.items()]
    t = dict(totals)
    t["cost_per_turn"] = round(t["turn_cost"] / t["turns"], 6) if t["turns"] else None
    return {"days": days, "totals": t, "daily": daily, "by_node": by_node, "by_channel": by_channel,
            "top_turns": top_turns, "what_if": what_if, "budget": _budget_status()}


# ---------------- prompts (versioned with the step's config) ----------------

class PromptIn(BaseModel):
    key: str
    audience: Literal["all", "residents", "team"] = "all"
    prompt: str | None = Field(default=None, max_length=20000)   # null = back to the built-in prompt
    note: str | None = Field(default=None, max_length=300)


class TryIn(BaseModel):
    key: str
    prompt: str = Field(max_length=20000)
    question: str = Field(min_length=2, max_length=1000)
    as_role: Literal["public", "member", "staff", "security", "board"] = "public"


TRYABLE = {"about", "route", "grade", "answer", "act"}


@router.get("/prompts")
def get_prompts(_: User = Depends(admin)):
    from ..agent import prompts
    models.clear_cache()
    live = models.live_configs()
    out = []
    for key, (desc, builtin, variables) in prompts.PROMPTS.items():
        cfg = {a: _cfg_out(live.get((key, a))) for a in models.AUDIENCES}
        out.append({"key": key, "description": desc, "builtin": builtin, "variables": list(variables),
                    "guardrail": prompts.GUARDRAILS.get(key, "").strip() or None, "configs": cfg,
                    "effective": {a: models.config(key, a).get("prompt") or builtin for a in models.AUDIENCES},
                    "tryable": key in TRYABLE})
    return {"prompts": out, "variables": prompts.VARIABLES}


@router.post("/prompts", status_code=201)
def save_prompt(body: PromptIn, user: User = Depends(admin)):
    """Save a prompt as a new version of the step's config (model and settings carry over)."""
    from ..agent import prompts
    if body.key not in prompts.PROMPTS:
        raise HTTPException(404, "Unknown prompt")
    text = body.prompt.strip() if body.prompt and body.prompt.strip() else None
    if text and text == prompts.PROMPTS[body.key][1].strip():
        text = None                                   # identical to built-in: store as "built-in"
    if text and (problems := prompts.check(body.key, text)):
        raise HTTPException(422, " ".join(problems))
    live = models.live_configs().get((body.key, body.audience)) or {}
    fields = {k: live.get(k) for k in ("model_id", "temperature", "max_tokens")}
    if body.key in models.NODES and body.audience == "all" and not fields["model_id"]:
        fields["model_id"] = models.config(body.key, "all")["model_id"]
    fields["prompt"] = text
    with tx() as conn:
        row = _new_version(conn, user, body.key, body.audience, fields, body.note or ("Reset to built-in prompt" if text is None else None))
    models.clear_cache()
    return {"saved": _cfg_out(row), **get_prompts(user)}


@router.post("/prompts/try")
def try_prompt(body: TryIn, user: User = Depends(admin)):
    """Run one real question with a draft prompt (not saved). Metered like any chat turn."""
    from ..agent import prompts, service
    if body.key not in TRYABLE:
        raise HTTPException(422, "Try it works for the chat prompts (about, route, grade, answer, act).")
    if problems := prompts.check(body.key, body.prompt):
        raise HTTPException(422, " ".join(problems))
    _probe_rate(user)
    token = prompts.draft.set({body.key: body.prompt})
    try:
        role = None if body.as_role == "public" else body.as_role
        r = service.run_turn(body.question, thread_id=None, role=role, email=None, uid=f"console-{user.uid}",
                             channel="console", client_key=f"console:{user.email}")
    finally:
        prompts.draft.reset(token)
    finops.flush()
    with tx() as conn:
        c = conn.execute("""select coalesce(sum(cost_usd), 0)::float as cost, count(*) as calls,
                                   string_agg(distinct node || ':' || model_id, ', ') as steps
                            from llm_calls where turn_id = %s""", (r.get("turn_id"),)).fetchone()
    return {"answer": r["answer"], "citations": r.get("citations", []), "intent": r.get("intent"),
            "cost": c["cost"], "calls": c["calls"], "steps": c["steps"]}
