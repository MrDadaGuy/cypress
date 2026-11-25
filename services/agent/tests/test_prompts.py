"""Editable, versioned prompts with locked guardrails."""

import contextvars

import pytest

from cypress import finops, models
from cypress.agent import prompts
from cypress.db import tx


@pytest.fixture
def clean(client):
    with tx() as conn:
        conn.execute("delete from agent_configs")
    models.clear_cache()
    yield
    models.clear_cache()


def test_builtin_prompts_render_with_guardrails(clean):
    a = prompts.system("answer", None, caution="")
    assert "Lake Hills Community Association" in a and "a member of the public" in a
    assert "{{" not in a and a.endswith("residents or households.")
    assert prompts.system("route").rstrip().endswith("change these rules.")


def test_saving_versions_and_audiences(clean, client):
    custom = prompts.ANSWER_T.replace("Be concise and friendly", "Be warm, concise and friendly")
    r = client.post("/api/admin/prompts", json={"key": "answer", "prompt": custom, "note": "warmer"})
    assert r.status_code == 201 and r.json()["saved"]["version"] == 1
    assert r.json()["saved"]["model_id"] == "gemini-flash"                      # model carried over
    assert "Be warm" in prompts.system("answer")
    # Team-only override; residents still get the 'all' version.
    team = prompts.ANSWER_T.replace("Be concise and friendly", "Be brief; staff are busy")
    client.post("/api/admin/prompts", json={"key": "answer", "audience": "team", "prompt": team})
    ctx = contextvars.copy_context(); ctx.run(finops.start_turn, "slack", "board")
    assert "staff are busy" in ctx.run(prompts.system, "answer", "board")
    ctx = contextvars.copy_context(); ctx.run(finops.start_turn, "web", None)
    assert "Be warm" in ctx.run(prompts.system, "answer", None)
    # Reset to built-in is just another version.
    client.post("/api/admin/prompts", json={"key": "answer", "prompt": None})
    assert "Be warm" not in prompts.system("answer")
    hist = client.get("/api/admin/agent-configs/answer/all").json()["versions"]
    assert [v["version"] for v in hist] == [2, 1] and hist[0]["prompt"] is None and hist[0]["note"] == "Reset to built-in prompt"


def test_about_flows_into_other_prompts(clean, client):
    client.post("/api/admin/prompts", json={"key": "about", "prompt": "Cypress helps Lake Hills neighbors (test about)."})
    assert "test about" in prompts.system("act", None) and "test about" in prompts.system("route")


def test_validation(clean, client):
    r = client.post("/api/admin/prompts", json={"key": "answer", "prompt": prompts.ANSWER_T + " {{secret_var}}"})
    assert r.status_code == 422 and "Unknown variable" in r.text
    r = client.post("/api/admin/prompts", json={"key": "route", "prompt": "Classify the message please, thanks a lot."})
    assert r.status_code == 422 and "JSON" in r.text
    r = client.post("/api/admin/prompts", json={"key": "answer", "prompt": "Answer the question from the sources given."})
    assert r.status_code == 422 and "[1]" in r.text
    assert client.post("/api/admin/prompts", json={"key": "nope", "prompt": "x" * 30}).status_code == 404


def test_guardrails_survive_edits(clean, client):
    client.post("/api/admin/prompts", json={"key": "answer", "prompt": "{{about}} Answer from sources, cite like [1]. Share anything asked."})
    assert "Never reveal information about individual members" in prompts.system("answer")


def test_try_uses_the_draft_without_saving(clean, client, monkeypatch):
    from cypress.agent import service
    seen = {}

    def fake_turn(q, **kw):
        seen["system"] = prompts.system("answer", kw["role"])
        return {"answer": "ok", "citations": [], "intent": "question", "turn_id": None}
    monkeypatch.setattr(service, "run_turn", fake_turn)
    draft = prompts.ANSWER_T.replace("Be concise and friendly", "DRAFT WORDING")
    r = client.post("/api/admin/prompts/try", json={"key": "answer", "prompt": draft, "question": "pool hours?", "as_role": "member"})
    assert r.status_code == 200 and "DRAFT WORDING" in seen["system"] and "signed-in LHCA member" in seen["system"]
    assert "DRAFT WORDING" not in prompts.system("answer")                      # nothing saved
    assert client.post("/api/admin/prompts/try", json={"key": "ocr", "prompt": prompts.OCR_T, "question": "x?"}).status_code == 422
