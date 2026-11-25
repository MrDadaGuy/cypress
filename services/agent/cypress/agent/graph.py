"""Cypress agent graph (LangGraph).

    route ──► retrieve ──► grade ──► answer
      │           ▲           │
      │           └─ rewrite ◄┘   (once, when the passages don't answer the question)
      ├─► act     (tool calling; tools offered depend on the caller's role, see tools.py)
      └─► reply   (greetings, off-topic, features not built yet)

Corrective RAG: a grader checks the retrieved passages before answering; if they miss, the
query is rewritten and retrieval runs again. Answers cite numbered sources, and the caller's
role limits which document sets retrieval can see.
"""

import logging
import operator
from typing import Annotated, Literal, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from .. import models, org
from ..retrieval import Passage, search
from . import prompts, tools

log = logging.getLogger(__name__)
HISTORY_TURNS = 6


class State(TypedDict, total=False):
    messages: Annotated[list[dict], operator.add]   # persisted conversation (checkpointed)
    role: str | None                                # caller's role; None = anonymous resident
    question: str                                   # standalone version of the latest message
    intent: str
    passages: list[dict]
    queries: list[str]
    grade: dict
    attempts: int
    answer: str
    citations: list[dict]
    models_used: dict
    tool_calls: list[dict]
    sign_in: dict | None                            # an action waiting for member sign-in (this turn only)
    uid: str | None
    email: str | None
    channel: str
    attachments: list[str]                          # photo upload ids sent with this message


def _emit(event: dict) -> None:
    """Send a progress event to a streaming caller; a no-op under plain invoke()."""
    try:
        get_stream_writer()(event)
    except Exception:
        pass


def _history(state: State) -> list[dict]:
    return state.get("messages", [])[-(HISTORY_TURNS * 2 + 1):]


def _used(state: State, node: str, model_id: str) -> dict:
    return {**state.get("models_used", {}), node: model_id}


def route(state: State) -> dict:
    text, mid = models.generate("route", prompts.system("route", state.get("role")), _history(state), json_mode=True, max_tokens=300, temperature=0)
    data = models.parse_json(text)
    intent = data.get("intent") if data.get("intent") in prompts.INTENTS else "question"
    if (state.get("role") not in ("board", "admin") and intent not in ("report_issue", "off_topic")
            and org.mentioned_board_member(state["messages"][-1]["content"])):
        intent = "board_contact"  # named a board member: always the volunteers/CAM reply
    question = (data.get("standalone_question") or state["messages"][-1]["content"]).strip()
    return {"intent": intent, "question": question, "attempts": 0, "queries": [], "passages": [],
            "grade": {}, "citations": [], "tool_calls": [], "sign_in": None, "models_used": _used(state, "route", mid)}


def retrieve(state: State) -> dict:
    q = state["queries"][-1] if state.get("queries") else state["question"]
    _emit({"status": "Searching LHCA documents…" if not state.get("attempts") else "Trying a different search…"})
    passages, _ = search(q, state.get("role"))
    return {"passages": [{**p.as_dict(), "content": p.content} for p in passages],
            "queries": (state.get("queries") or []) + ([q] if not state.get("queries") else [])}


def grade(state: State) -> dict:
    if not state["passages"]:
        return {"grade": {"sufficient": False, "better_query": state["question"]}}
    _emit({"status": "Checking the sources…"})
    msg = prompts.grade_input(state["question"], state["passages"])
    text, mid = models.generate("grade", prompts.system("grade", state.get("role")), [{"role": "user", "content": msg}],
                                json_mode=True, max_tokens=200, temperature=0)
    g = models.parse_json(text)
    return {"grade": {"sufficient": bool(g.get("sufficient")), "better_query": g.get("better_query") or ""},
            "models_used": _used(state, "grade", mid)}


def rewrite(state: State) -> dict:
    better = state["grade"].get("better_query") or state["question"]
    return {"attempts": state.get("attempts", 0) + 1, "queries": state["queries"] + [better]}


def answer(state: State) -> dict:
    sources = state["passages"]
    msgs = _history(state)[:-1] + [{"role": "user", "content": prompts.answer_input(state["question"], sources)}]
    system = prompts.answer_system(state.get("role"), bool(state["grade"].get("sufficient")))
    _emit({"status": "Writing the answer…"})
    text, mid = models.generate_stream("answer", system, msgs, lambda t: _emit({"token": t}),
                                       on_reset=lambda: _emit({"reset": True}), max_tokens=1200, temperature=0.2)
    text = prompts.normalize_citations(text)
    cited = prompts.cited_numbers(text)
    citations = [{"n": i + 1, "title": s["title"], "uri": s["uri"], "section": s["section"],
                  "set": s["set_name"]} for i, s in enumerate(sources) if (i + 1) in cited]
    return {"answer": text, "citations": citations, "messages": [{"role": "assistant", "content": text}],
            "models_used": _used(state, "answer", mid)}


def act(state: State) -> dict:
    ctx = tools.Ctx(role=state.get("role"), uid=state.get("uid"), channel=state.get("channel", ""),
                    email=state.get("email"), attachments=tuple(state.get("attachments") or ()))
    offered = tools.for_role(ctx.role)
    _emit({"status": "Looking that up…"})

    def run_tool(name: str, args: dict) -> dict:
        t = tools.TOOLS.get(name)
        _emit({"status": t.status if t else "Looking that up…"})
        return tools.call(name, args, ctx)

    msgs = _history(state)[:-1] + [{"role": "user", "content": state["question"]}]
    text, mid, calls = models.generate_with_tools(
        "act", prompts.act_system(ctx.role), msgs, offered, run_tool,
        max_tokens=800)
    if not text:
        text = prompts.canned(state["intent"], ctx.role)
    _emit({"token": text})
    log_calls = [{"tool": c["tool"], "args": c["args"],
                  "ok": "error" not in c["result"]} for c in calls]
    waiting = next((c["result"] for c in calls if c["result"].get("needs_member_sign_in")), None)
    sign_in = {"pending_id": waiting["pending_id"], "title": waiting.get("draft_title")} if waiting else None
    return {"answer": text, "citations": [], "tool_calls": log_calls, "sign_in": sign_in,
            "messages": [{"role": "assistant", "content": text}], "models_used": _used(state, "act", mid)}


def reply(state: State) -> dict:
    if state["intent"] == "board_contact":
        text = org.board_reply(org.mentioned_board_member(state["messages"][-1]["content"]))
    else:
        text = prompts.canned(state["intent"], state.get("role"))
    _emit({"token": text})
    return {"answer": text, "citations": [], "messages": [{"role": "assistant", "content": text}]}


def after_route(state: State) -> Literal["retrieve", "act", "reply"]:
    if state["intent"] == "question":
        return "retrieve"
    if state["intent"] == "tickets" and state.get("role") not in tools.STAFF:
        return "reply"
    if state["intent"] == "member_lookup" and not tools.TOOLS["lookup_member"].allows(state.get("role")):
        return "reply"   # members, the public, or a role an admin turned off: a fixed, safe reply
    if state["intent"] in prompts.TOOL_INTENTS and tools.for_role(state.get("role")):
        return "act"
    return "reply"


def after_grade(state: State) -> Literal["answer", "rewrite"]:
    g = state["grade"]
    if g.get("sufficient") or state.get("attempts", 0) >= 1:
        return "answer"
    return "rewrite"


def _node(name: str, fn):
    """Run a graph step inside a trace span, tagged with what it decided."""
    import functools

    from .. import tracing

    @functools.wraps(fn)
    def inner(state: State):
        with tracing.span(f"node {name}", **{"langgraph.node": name}):
            out = fn(state) or {}
            tracing.set_attrs(**{f"cypress.{k}": out[k] for k in ("intent", "attempts") if k in out},
                              **({"cypress.passages": len(out["passages"])} if "passages" in out else {}),
                              **({"cypress.sufficient": bool(out["grade"].get("sufficient"))}
                                 if isinstance(out.get("grade"), dict) else {}),
                              **({"cypress.tools": ",".join(c.get("tool", "") for c in out["tool_calls"])}
                                 if out.get("tool_calls") else {}))
            return out
    return inner


def build(checkpointer=None):
    g = StateGraph(State)
    for name, fn in [("route", route), ("retrieve", retrieve), ("grade", grade), ("rewrite", rewrite),
                     ("answer", answer), ("act", act), ("reply", reply)]:
        g.add_node(name, _node(name, fn))
    g.add_edge(START, "route")
    g.add_conditional_edges("route", after_route)
    g.add_edge("retrieve", "grade")
    g.add_conditional_edges("grade", after_grade)
    g.add_edge("rewrite", "retrieve")
    g.add_edge("answer", END)
    g.add_edge("act", END)
    g.add_edge("reply", END)
    return g.compile(checkpointer=checkpointer)


__all__ = ["build", "State", "Passage"]
