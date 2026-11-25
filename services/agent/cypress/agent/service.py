"""Run one chat turn: rate limit, invoke the graph with a Postgres checkpointer, log the interaction."""

import contextvars
import logging
import time
import uuid

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from ..config import settings
from .. import finops, uploads
from ..db import tx
from .graph import build

log = logging.getLogger(__name__)
MAX_CHARS = 2000

# Per-key limits: (window seconds, max requests). Anonymous traffic is keyed by IP. Chat limits for
# anonymous and signed-in callers are admin settings (protection.py); these are for internal kinds.
LIMITS = {
    "client_error": [(3600, 30), (86400, 100)],   # browser error reports, per IP
    "probe": [(3600, 30), (86400, 100)],          # admin "Test" calls on models (each is a paid call)
    "eval_run": [(3600, 6), (86400, 20)],         # eval runs (each is ~30 paid chat turns, ~$0.25)
}

_graph = None
_cp_pool: ConnectionPool | None = None


def graph():
    """The compiled graph with a Postgres checkpointer (separate autocommit pool, as LangGraph requires)."""
    global _graph, _cp_pool
    if _graph is None:
        _cp_pool = ConnectionPool(settings.database_url, min_size=1, max_size=3, open=True,
                                  kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
        saver = PostgresSaver(_cp_pool)
        saver.setup()
        _graph = build(saver)
    return _graph


def close() -> None:
    if _cp_pool:
        _cp_pool.close()


class RateLimited(Exception):
    pass


def check_rate(key: str, kind: str, rules: list[tuple[int, int]] | None = None) -> None:
    from .. import protection
    rules = rules or (protection.limits(kind) if kind in ("anon", "user") else LIMITS[kind])
    with tx() as conn:
        conn.execute("delete from rate_events where key = %s and at < now() - interval '1 day'", (key,))
        for window, limit in rules:
            n = conn.execute("select count(*) as n from rate_events where key = %s and at > now() - make_interval(secs => %s)",
                             (key, window)).fetchone()["n"]
            if n >= limit:
                period = "hour" if window == 3600 else "day"
                break
        else:
            conn.execute("insert into rate_events (key) values (%s)", (key,))
            return
    protection.record(f"rate_{period}", key.split(":")[0], key)
    raise RateLimited(period)


def run_turn(message: str, *, thread_id: str | None, role: str | None, email: str | None,
             uid: str | None, channel: str, client_key: str, attachments: list[str] | None = None) -> dict:
    """One full turn (Slack, tests). Raises RateLimited."""
    check_rate(f"chat:{uid or client_key}", "user" if uid else "anon")
    done: dict = {}
    for ev in stream_turn(message, thread_id=thread_id, role=role, email=email, uid=uid, channel=channel,
                          attachments=attachments):
        if ev["type"] == "done":
            done = ev
    return {k: v for k, v in done.items() if k != "type"}


def stream_turn(message: str, *, thread_id: str | None, role: str | None, email: str | None,
                uid: str | None, channel: str, attachments: list[str] | None = None):
    """Run one turn, yielding progress: {"type": "status"|"token"|"reset"|"done", ...}.
    The caller does rate limiting first, so a 429 can be returned before streaming starts."""
    message = message.strip()[:MAX_CHARS]
    photos = uploads.usable(attachments or [])
    if photos:
        message += f"\n[Attached {len(photos)} photo{'s' if len(photos) > 1 else ''} or video{'s' if len(photos) > 1 else ''}]"
    thread_id = thread_id or uuid.uuid4().hex
    # Namespacing by user keeps one caller from reading another's thread by guessing its id.
    config = {"configurable": {"thread_id": f"{channel}:{uid or 'anon'}:{thread_id}"}, "recursion_limit": 12}
    started = time.monotonic()
    result, error = {}, None
    # Every model call in this turn is tagged with the turn id (FinOps). The graph runs inside its own
    # context so the tag holds no matter which thread pulls the next event.
    tctx = contextvars.copy_context()
    turn_id = tctx.run(finops.start_turn, channel, role)
    # One trace span for the whole turn, current inside tctx so every step and model call nests under it.
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace

    from .. import tracing
    turn_span = tracing.tracer.start_span("chat turn", attributes={"cypress.channel": channel,
                                                                    "cypress.role": role or "anonymous"})
    tctx.run(otel_context.attach, otel_trace.set_span_in_context(turn_span))
    trace_id = format(turn_span.get_span_context().trace_id, "032x") if turn_span.get_span_context().is_valid else None
    try:
        inputs = {"messages": [{"role": "user", "content": message}], "role": role, "uid": uid, "email": email,
                  "channel": channel, "attachments": photos}
        events = graph().stream(inputs, config, stream_mode=["custom", "values"])
        while True:
            try:
                mode, data = tctx.run(next, events)
            except StopIteration:
                break
            if mode == "values":
                result = data
            elif "token" in data:
                yield {"type": "token", "text": data["token"]}
            elif "status" in data:
                yield {"type": "status", "text": data["status"]}
            elif data.get("reset"):
                yield {"type": "reset"}
    except Exception as e:
        log.exception("chat turn failed")
        error = f"{type(e).__name__}: {e}"
    latency = int((time.monotonic() - started) * 1000)
    turn_span.set_attributes({k: v for k, v in {"cypress.intent": result.get("intent"), "cypress.error": bool(error),
                                                 "cypress.latency_ms": latency}.items() if v is not None})
    if error:
        from opentelemetry.trace import Status, StatusCode
        turn_span.set_status(Status(StatusCode.ERROR, error[:200]))
    turn_span.end()
    # One structured line per turn (no question text): Cloud Monitoring charts turns, intents and latency from it.
    log.info("chat turn", extra={"json_fields": {"cypress.event": "chat_turn", "cypress.channel": channel,
                                                 "cypress.intent": result.get("intent") or "none",
                                                 "cypress.role": role or "anonymous", "cypress.error": bool(error),
                                                 "latency_ms": latency}})
    answer = result.get("answer") if not error else None
    answer = answer or "Sorry, something went wrong on my side. Please try again in a moment."
    turn_id = _log(channel, thread_id, role, email, message, result, answer, latency, error, turn_id=turn_id,
                   trace_id=trace_id)
    done = {"type": "done", "thread_id": thread_id, "turn_id": turn_id, "answer": answer,
            "citations": result.get("citations", []), "intent": result.get("intent"), "error": bool(error)}
    if not error and role is None and channel == "web":
        if result.get("sign_in"):
            done["sign_in"] = result["sign_in"]
        else:
            # A gentle prompt after a few questions (admin setting; 0 = never).
            from .. import members
            n = int(members.auth_settings().get("nudge_after_turns") or 0)
            asked = sum(1 for m in result.get("messages", []) if m.get("role") == "user")
            if n and asked == n:
                done["nudge_sign_in"] = True
    yield done


def record_feedback(turn_id: str, rating: int | None, comment: str | None, by: str) -> bool:
    """Set (or change) the rating on a turn; a comment alone keeps the existing rating."""
    with tx() as conn:
        row = conn.execute(
            """update interactions set rating = coalesce(%s, rating),
                 feedback_comment = coalesce(%s, feedback_comment), feedback_by = %s, feedback_at = now()
               where public_id = %s returning id""",
            (rating, (comment or "").strip()[:1000] or None, by, turn_id),
        ).fetchone()
    return row is not None


def _log(channel, thread_id, role, email, question, r, answer, latency, error, turn_id=None,
         trace_id=None) -> str | None:
    try:
        retrieval = {"queries": r.get("queries", []), "grade": r.get("grade", {}),
                     "passages": [{k: p.get(k) for k in ("title", "section", "set_slug", "score")}
                                  for p in r.get("passages", [])],
                     "tool_calls": r.get("tool_calls", [])}
        with tx() as conn:
            return str(conn.execute(
                """insert into interactions (public_id, channel, thread_id, user_role, user_email, question, intent,
                     answer, citations, retrieval, models, latency_ms, error, trace_id)
                   values (coalesce(%s::uuid, gen_random_uuid()), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning public_id""",
                (turn_id, channel, thread_id, role, email, question, r.get("intent"), answer, Jsonb(r.get("citations", [])),
                 Jsonb(retrieval), Jsonb(r.get("models_used", {})), latency, error, trace_id),
            ).fetchone()["public_id"])
    except Exception:
        log.exception("logging interaction failed")
        return None
