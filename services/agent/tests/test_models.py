"""Model catalog, versioned agent configs, metering (no FinOps leaks) and the budget guard."""

import contextvars

import pytest

from cypress import finops, models
from cypress.db import tx


@pytest.fixture
def clean(client, monkeypatch):
    with tx() as conn:
        conn.execute("delete from llm_calls")
        conn.execute("delete from agent_configs")
        conn.execute("update llm_models set enabled = (id not like 'claude-%%' and id <> 'semantic-ranker')")
        conn.execute("delete from org_settings where key in ('llm_budget', 'llm_runtime')")
    from cypress import org
    org.clear()
    models.clear_cache()

    def fake_call(m, system, messages, json_mode, max_tokens, temperature):
        fake_call.seen.append((m.id, max_tokens, temperature))
        return f"reply from {m.id}", finops.Usage(input_tokens=1000, cached_tokens=200, output_tokens=500)
    fake_call.seen = []
    monkeypatch.setattr(models, "_call", fake_call)
    yield fake_call
    models.clear_cache()


def calls():
    finops.flush()
    with tx() as conn:
        return conn.execute("select * from llm_calls order by id").fetchall()


def test_every_call_is_metered_with_cost(clean):
    text, mid = models.generate("route", "sys", [{"role": "user", "content": "hi"}])
    assert (text, mid) == ("reply from gemini-flash-lite", "gemini-flash-lite")
    [row] = calls()
    # Flash-Lite: 800 fresh input × $0.30 + 200 cached × $0.03 + 500 output × $2.50, per 1M
    assert float(row["cost_usd"]) == pytest.approx((800 * 0.30 + 200 * 0.03 + 500 * 2.50) / 1e6)
    assert (row["node"], row["purpose"], row["ok"]) == ("route", "primary", True)


def test_failed_primary_and_fallback_are_both_metered(clean, client):
    def flaky(m, *a):
        if m.id == "gemini-flash":
            raise RuntimeError("boom")
        return "ok", finops.Usage(input_tokens=10, output_tokens=5)
    models._call = flaky  # monkeypatched by the fixture, restored after
    client.post("/api/admin/agent-configs", json={"node": "route", "model_id": "gemini-flash"})
    text, mid = models.generate("route", "sys", [{"role": "user", "content": "hello there"}])
    assert mid == "gemini-flash-lite"
    rows = calls()
    assert [(r["model_id"], r["purpose"], r["ok"]) for r in rows] == [
        ("gemini-flash", "primary", False), ("gemini-flash-lite", "fallback", True)]
    assert rows[0]["estimated"] and rows[0]["cost_usd"] is not None


def test_configs_are_versioned_and_restorable(clean, client):
    r = client.post("/api/admin/agent-configs", json={"node": "answer", "model_id": "gpt-oss-120b", "note": "try oss"})
    assert r.status_code == 201 and r.json()["saved"]["version"] == 1
    client.post("/api/admin/agent-configs", json={"node": "answer", "model_id": "gemini-flash", "temperature": 0.5})
    assert models.model_for("answer").id == "gemini-flash"
    models.generate("answer", "s", [{"role": "user", "content": "q"}], temperature=0.2, max_tokens=99)
    assert clean.seen[-1] == ("gemini-flash", 99, 0.5)          # config temperature wins, max_tokens kept
    hist = client.get("/api/admin/agent-configs/answer/all").json()["versions"]
    assert [v["version"] for v in hist] == [2, 1]
    assert hist[0]["calls"] == 1 and hist[0]["cost"] > 0         # spend is tied to the version that ran
    v1 = hist[1]["id"]
    r = client.post(f"/api/admin/agent-configs/{v1}/restore", json={})
    assert r.json()["saved"]["version"] == 3 and r.json()["saved"]["model_id"] == "gpt-oss-120b"
    assert models.model_for("answer").id == "gpt-oss-120b"


def test_audience_overrides(clean, client):
    client.post("/api/admin/agent-configs", json={"node": "answer", "audience": "team", "model_id": "llama-4-maverick"})
    assert models.model_for("answer", "team").id == "llama-4-maverick"
    assert models.model_for("answer", "residents").id == "gemini-flash"   # falls back to 'all' / built-in
    ctx = contextvars.copy_context()
    ctx.run(finops.start_turn, "slack", "board")
    assert ctx.run(models.model_for, "answer").id == "llama-4-maverick"
    ctx2 = contextvars.copy_context()
    ctx2.run(finops.start_turn, "web", None)
    assert ctx2.run(models.model_for, "answer").id == "gemini-flash"


def test_turn_tag_follows_hedged_calls_onto_worker_threads(clean):
    ctx = contextvars.copy_context()
    tid = ctx.run(finops.start_turn, "web", None)
    ctx.run(models.generate, "grade", "s", [{"role": "user", "content": "q"}])
    [row] = calls()
    assert str(row["turn_id"]) == tid and row["channel"] == "web" and row["user_role"] is None


def test_guardrails_on_the_catalog(clean, client):
    haiku = next(m for m in client.get("/api/admin/models").json()["models"] if m["id"] == "claude-haiku")
    r = client.put("/api/admin/models/claude-haiku", json={**haiku, "enabled": True})
    assert r.status_code == 422 and "prices" in r.text                     # no unmetered models
    r = client.post("/api/admin/agent-configs", json={"node": "answer", "model_id": "claude-sonnet"})
    assert r.status_code == 422                                            # switched off
    sonnet = next(m for m in client.get("/api/admin/models").json()["models"] if m["id"] == "claude-sonnet")
    assert client.put("/api/admin/models/claude-sonnet", json={**sonnet, "enabled": True}).status_code == 200
    r = client.post("/api/admin/agent-configs", json={"node": "act", "model_id": "claude-sonnet"})
    assert r.status_code == 422 and "Gemini" in r.text                     # tool calling is Gemini-only
    flash = next(m for m in client.get("/api/admin/models").json()["models"] if m["id"] == "gemini-flash-lite")
    r = client.put("/api/admin/models/gemini-flash-lite", json={**flash, "enabled": False})
    assert r.status_code == 409                                            # it's the fallback model


def test_budget_guard_drops_to_the_cheap_model(clean, client):
    rt = {"fallback_model": "gemini-flash-lite", "hedge_s": 4, "stream_hedge_s": 5, "timeout_s": 25}
    client.put("/api/admin/llm-settings", json={"runtime": rt, "budget": {
        "monthly_usd": 0.01, "guard": True, "guard_model": "gemini-flash-lite", "alert_pct": 80}})
    assert models.model_for("answer").id == "gemini-flash"
    with tx() as conn:
        conn.execute("""insert into llm_calls (node, model_id, provider, cost_usd) values ('answer', 'gemini-flash', 'gemini', 0.02)""")
    finops.clear()
    assert models.model_for("answer").id == "gemini-flash-lite"
    b = client.get("/api/admin/models").json()["budget"]
    assert b["guard_active"] and b["alert"]


def test_unpriced_calls_can_be_repriced(clean, client):
    with tx() as conn:
        conn.execute("""insert into llm_calls (node, model_id, provider, input_tokens, output_tokens)
                        values ('answer', 'gemini-flash', 'gemini', 1000000, 0)""")
    assert client.post("/api/admin/models/gemini-flash/reprice").json() == {"repriced": 1}
    assert float(calls()[0]["cost_usd"]) == pytest.approx(1.50)


def test_usage_report(clean, client):
    models.generate("route", "s", [{"role": "user", "content": "q"}])
    u = client.get("/api/admin/usage?days=7").json()
    assert u["totals"]["calls"] == 1 and u["totals"]["unpriced"] == 0
    route = next(w for w in u["what_if"] if w["node"] == "route")
    assert route["options"][0]["model_id"] == "gpt-oss-120b"                # cheapest for the same tokens


def test_one_reranker_at_a_time_and_local_rerank_is_metered_free(clean, client, monkeypatch):
    from cypress import retrieval
    vertex = next(m for m in client.get("/api/admin/models").json()["models"] if m["id"] == "semantic-ranker")
    d = client.put("/api/admin/models/semantic-ranker", json={**vertex, "enabled": True}).json()
    on = [m["id"] for m in d["models"] if m["kind"] == "rerank" and m["enabled"]]
    assert on == ["semantic-ranker"]                                        # flashrank switched off
    local = next(m for m in d["models"] if m["id"] == "flashrank-tinybert")
    client.put("/api/admin/models/flashrank-tinybert", json={**local, "enabled": True})
    assert models.rerank_model().id == "flashrank-tinybert"

    monkeypatch.setattr(retrieval, "_local_scores", lambda m, q, ps: {i: float(i) for i in range(len(ps))})
    ps = [retrieval.Passage(i, 0, f"t{i}", "", "", "", "", "rules", 1, "x") for i in range(4)]
    out = retrieval._rerank("q", ps, 2)
    assert [p.chunk_id for p in out] == [3, 2]
    [row] = calls()
    assert (row["node"], row["model_id"], float(row["cost_usd"]), row["ok"]) == ("rerank", "flashrank-tinybert", 0.0, True)


def test_failed_rerank_falls_back_and_costs_nothing(clean, monkeypatch):
    from cypress import retrieval
    monkeypatch.setattr(retrieval, "_local_scores", lambda *a: (_ for _ in ()).throw(RuntimeError("oom")))
    assert retrieval._rerank("q", [retrieval.Passage(1, 0, "t", "", "", "", "", "rules", 1, "x")], 1) is None
    [row] = calls()
    assert not row["ok"] and float(row["cost_usd"]) == 0
