"""LLM FinOps: meter every paid model call, price it, and enforce the monthly budget.

Principle: no FinOps leaking. Every provider call (primary, hedge, fallback, failed, test, embedding)
goes through record(), and calls without a known price are stored with cost null so the console can
flag them and re-price them later. Cost data is admin-only.
"""

import contextvars
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .db import tx

log = logging.getLogger("cypress.finops")

# Who/what a call belongs to: set once per chat turn, read on whatever thread makes the call.
_turn: contextvars.ContextVar[dict | None] = contextvars.ContextVar("llm_turn", default=None)
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="finops")   # one writer: flush() waits for all


@dataclass
class Usage:
    input_tokens: int = 0       # includes cached
    cached_tokens: int = 0
    output_tokens: int = 0      # includes thinking
    thinking_tokens: int = 0
    requests: int = 1           # for per-request pricing
    estimated: bool = False


def estimate_tokens(*texts: str) -> int:
    return max(1, sum(len(t or "") for t in texts) // 4)


def start_turn(channel: str, role: str | None) -> str:
    """Tag later model calls in the current context with a new turn id (becomes interactions.public_id).
    Call it inside a copied context (ctx.run) so the tag ends with the turn."""
    tid = str(uuid.uuid4())
    _turn.set({"turn_id": tid, "channel": channel, "user_role": role})
    return tid


def audience() -> str:
    """Which agent config audience the current call belongs to: 'residents' for anonymous chat,
    'team' for signed-in roles, 'all' for background work (ingest, scheduled jobs)."""
    t = _turn.get()
    if t is None:
        return "all"
    return "team" if t.get("user_role") else "residents"


def context() -> contextvars.Context:
    """Snapshot for handing to worker threads, so their calls stay attributed to the turn."""
    return contextvars.copy_context()


def cost(model, u: Usage) -> float | None:
    """USD for one call, or None if the model has no price."""
    if model is None or not model.priced:
        return None
    cached = min(u.cached_tokens, u.input_tokens)
    in_p, out_p = model.input_per_m or 0, model.output_per_m or 0
    cached_p = model.cached_per_m if model.cached_per_m is not None else in_p
    return (((u.input_tokens - cached) * in_p + cached * cached_p + u.output_tokens * out_p) / 1_000_000
            + u.requests * (model.per_call_usd or 0))


def record(model, node: str, purpose: str, u: Usage, latency_ms: int | None, ok: bool = True,
           error: str | None = None, config_id: int | None = None) -> None:
    """Meter one call. Never raises; the write happens off the request path."""
    try:
        t = _turn.get() or {}
        c = cost(model, u)
        row = (node, model.id, model.provider, purpose, u.input_tokens, u.cached_tokens, u.output_tokens,
               u.thinking_tokens, c, u.estimated, latency_ms, ok, (error or "")[:300] or None, config_id,
               t.get("turn_id"), t.get("channel"), t.get("user_role"))
        # Structured log line too: Cloud Logging can chart these (log-based metrics) at no extra cost.
        log.info("llm call %s %s %s", node, model.id, purpose, extra={"json_fields": {
            "gen_ai.system": model.provider, "gen_ai.request.model": model.name, "gen_ai.operation.name": node,
            "gen_ai.usage.input_tokens": u.input_tokens, "gen_ai.usage.output_tokens": u.output_tokens,
            "cypress.cost_usd": c, "cypress.purpose": purpose, "cypress.ok": ok, "latency_ms": latency_ms}})
        _trace(model, node, purpose, u, c, ok, latency_ms)
        _writer.submit(_write, row)
        if c:
            _add_spend(c)
    except Exception:
        log.exception("metering failed for %s", getattr(model, "id", model))


def _trace(model, node: str, purpose: str, u: Usage, c: float | None, ok: bool, latency_ms: int | None) -> None:
    """Usage on the current model-call span; calls without one (embeddings, reranking) get their own span."""
    import time

    from opentelemetry import trace

    from .tracing import set_attrs, tracer
    attrs = {"gen_ai.usage.input_tokens": u.input_tokens, "gen_ai.usage.output_tokens": u.output_tokens,
             "gen_ai.usage.cached_tokens": u.cached_tokens, "cypress.cost_usd": c, "cypress.ok": ok,
             "cypress.estimated": u.estimated}
    cur = trace.get_current_span()
    if getattr(cur, "name", "").startswith("llm "):
        set_attrs(**attrs)
    elif cur.get_span_context().is_valid:
        end = time.time_ns()
        sp = tracer.start_span(f"llm {node}", start_time=end - (latency_ms or 0) * 1_000_000, attributes={
            "gen_ai.system": model.provider, "gen_ai.request.model": model.name, "gen_ai.operation.name": node,
            "cypress.purpose": purpose, **{k: v for k, v in attrs.items() if v is not None}})
        sp.end(end_time=end)


INSERT = """insert into llm_calls (node, model_id, provider, purpose, input_tokens, cached_tokens, output_tokens,
              thinking_tokens, cost_usd, estimated, latency_ms, ok, error, config_id, turn_id, channel, user_role)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""


def _write(row: tuple) -> None:
    try:
        with tx() as conn:
            conn.execute(INSERT, row)
    except Exception:
        log.exception("writing llm_calls failed")


def flush(timeout: float = 5.0) -> None:
    """Wait for pending ledger writes (tests, shutdown)."""
    _writer.submit(lambda: None).result(timeout=timeout)


# ---------------- budget ----------------

BUDGET_DEFAULTS = {"monthly_usd": 15, "guard": True, "guard_model": "gemini-flash-lite", "alert_pct": 80}
_spend: dict = {"at": 0.0, "month": None, "usd": 0.0}
SPEND_TTL = 60


def budget() -> dict:
    from . import org
    return {**BUDGET_DEFAULTS, **(org.get().get("llm_budget") or {})}


def month_to_date() -> float:
    """Spend this calendar month (America/Chicago), cached for a minute and bumped by each call."""
    now = time.time()
    month = time.strftime("%Y-%m")
    if now - _spend["at"] < SPEND_TTL and _spend["month"] == month:
        return _spend["usd"]
    try:
        with tx() as conn:
            usd = conn.execute(
                """select coalesce(sum(cost_usd), 0)::float as usd from llm_calls
                   where at >= date_trunc('month', now() at time zone 'America/Chicago') at time zone 'America/Chicago'"""
            ).fetchone()["usd"]
        _spend.update(at=now, month=month, usd=usd)
    except Exception:
        log.exception("reading month-to-date spend failed")
    return _spend["usd"]


def _add_spend(c: float) -> None:
    _spend["usd"] = _spend.get("usd", 0.0) + c


def clear() -> None:
    _spend.update(at=0.0, month=None, usd=0.0)


def guard_model() -> str | None:
    """If the budget guard has tripped, the model id every node should use instead; else None."""
    b = budget()
    if not b.get("guard") or not b.get("monthly_usd"):
        return None
    if month_to_date() < float(b["monthly_usd"]):
        return None
    return b.get("guard_model") or BUDGET_DEFAULTS["guard_model"]
