"""Evals: checks, runs with overrides, dry-run tools, judge scoring."""

import pytest

from cypress import evals, models
from cypress.agent import prompts, service, tools
from cypress.db import tx


@pytest.fixture
def clean(client, monkeypatch):
    with tx() as conn:
        conn.execute("delete from eval_runs")
        conn.execute("delete from eval_cases")
        conn.execute("delete from rate_events where key like 'eval_run:%%'")
    monkeypatch.setattr(evals, "enqueue", lambda r, c: evals.run_case(r, c))    # run inline
    seen = []

    def fake_turn(question, **kw):
        seen.append({"q": question, "role": kw["role"], "channel": kw["channel"], "dry": tools.DRY_RUN.get(),
                     "draft": dict(prompts.draft.get()), "models": dict(models.override.get())})
        answer = "The pool is closed on Mondays for maintenance [1]." if "Monday" in question else "Hello!"
        yield {"type": "done", "answer": answer, "intent": "question" if "Monday" in question else "greeting",
               "citations": [{"n": 1, "title": "Swimming Pool"}] if "Monday" in question else [], "turn_id": None, "error": False}
    monkeypatch.setattr(service, "stream_turn", fake_turn)
    monkeypatch.setattr(models, "generate", lambda *a, **k: ('{"score": 5, "verdict": "pass", "reason": "fine"}', "gemini-flash"))
    yield seen


def test_checks():
    e = {"intent": "question", "must_include": [["monday"], ["closed", "shut"]], "must_not_include": ["open daily"],
         "cite": ["Swimming Pool"], "tool": "report_issue"}
    c = evals.checks(e, "Closed on Mondays.", "question", [{"title": "LHCA Swimming Pool"}], [])
    assert c["intent"]["ok"] and c["includes 1"]["ok"] and c["includes 2"]["ok"] and c["excludes"]["ok"] and c["cites"]["ok"]
    assert not c["tool"]["ok"]
    c = evals.checks(e, "Open daily", "greeting", [], ["report_issue"])
    assert not c["intent"]["ok"] and not c["includes 1"]["ok"] and not c["excludes"]["ok"] and not c["cites"]["ok"] and c["tool"]["ok"]


def test_starter_set_is_seeded_once(clean, client):
    with tx() as conn:
        conn.execute("delete from org_settings where key = 'eval_seed_batches'")
    total = len(evals.SEED) + sum(len(c) for c in evals.BATCHES.values())
    d = client.get("/api/admin/evals").json()
    assert d["seeded"] == total and len(d["cases"]) == total
    assert client.get("/api/admin/evals").json()["seeded"] == 0


def test_run_with_overrides_dry_run_and_scoring(clean, client):
    a = client.post("/api/admin/evals/cases", json={"question": "Is the pool open on Monday?", "tags": ["pool"],
                    "expect": {"intent": "question", "must_include": [["monday"]], "cite": ["Swimming Pool"]}}).json()["id"]
    b = client.post("/api/admin/evals/cases", json={"question": "hello", "as_role": "staff", "tags": ["routing"],
                    "expect": {"intent": "greeting", "must_include": [["goodbye"]]}}).json()["id"]
    draft = prompts.ANSWER_T.replace("Be concise", "Be very concise")
    r = client.post("/api/admin/evals/runs", json={"label": "try flash-lite", "prompts": {"answer": draft},
                                                    "models": {"answer": "gemini-flash-lite"}, "case_ids": [a, b]})
    assert r.status_code == 201
    run = client.get(f"/api/admin/evals/runs/{r.json()['id']}").json()
    assert run["status"] == "done" and run["cases"] == 2 and run["passed"] == 1 and run["avg_score"] == 5
    fail = next(x for x in run["results"] if x["case_id"] == b)
    assert fail["passed"] is False and not fail["checks"]["includes 1"]["ok"]
    s = clean[0]
    assert s["dry"] and s["channel"] == "eval" and s["draft"] == {"answer": draft} and s["models"] == {"answer": "gemini-flash-lite"}
    assert next(x for x in clean if x["q"] == "hello")["role"] == "staff"
    assert prompts.draft.get() == {} and models.override.get() == {}         # nothing leaks out of the run


def test_run_validation(clean, client):
    client.post("/api/admin/evals/cases", json={"question": "hi there"})
    assert client.post("/api/admin/evals/runs", json={"label": "x", "prompts": {"answer": "too short"}}).status_code == 422
    assert client.post("/api/admin/evals/runs", json={"label": "x", "models": {"answer": "claude-haiku"}}).status_code == 422
    assert client.post("/api/admin/evals/runs", json={"label": "x", "models": {"act": "gpt-oss-120b"}}).status_code == 422


def test_report_tool_dry_run_files_nothing(clean, monkeypatch):
    from cypress import tickets
    monkeypatch.setattr(tickets, "create", lambda **kw: (_ for _ in ()).throw(AssertionError("filed for real")))
    tok = tools.DRY_RUN.set(True)
    try:
        r = tools.call("report_issue", {"title": "t", "description": "d", "category": "pool", "urgency": "low"},
                       tools.Ctx(role="member", uid="u", channel="web", email="m@x.test"))
        assert r["ticket_number"] == 9999
        r = tools.call("report_issue", {"title": "t", "description": "d", "category": "pool", "urgency": "low"},
                       tools.Ctx(role=None, uid=None, channel="web"))
        assert r["needs_member_sign_in"]
    finally:
        tools.DRY_RUN.reset(tok)
    with tx() as conn:
        assert conn.execute("select count(*) as n from pending_actions where payload->>'title' = 't'").fetchone()["n"] == 0
