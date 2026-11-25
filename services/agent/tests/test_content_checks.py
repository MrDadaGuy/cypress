"""Content checks: conflict detection filters, issue lifecycle, digest line."""

import pytest

from cypress import consistency, digest, models
from cypress.db import tx


@pytest.fixture
def clean(client, monkeypatch):
    with tx() as conn:
        conn.execute("delete from content_issues")
        conn.execute("delete from content_scans")
    yield monkeypatch


def pair(a_doc=1, b_doc=2, a="Dues are $650.", b="Dues are $700."):
    return {"a_id": 10, "b_id": 20, "a_doc": a_doc, "b_doc": b_doc, "a_title": "Bylaws", "b_title": "Membership",
            "a_uri": "u1", "b_uri": "u2", "a_content": a, "b_content": b, "sim": 0.9}


def fake_llm(monkeypatch, payload):
    monkeypatch.setattr(models, "generate", lambda *a, **k: (payload, "gemini-flash-lite"))


def test_silence_and_vague_claims_are_not_conflicts(clean):
    fake_llm(clean, '{"conflict": true, "topic": "dues", "a_says": "Passage A does not mention dues", "b_says": "$700"}')
    assert consistency.check_pair(pair()) is None
    fake_llm(clean, '{"conflict": true, "topic": "dues", "a_says": "$650 per year", "b_says": "$700 per year", "severity": "high"}')
    assert consistency.check_pair(pair())["severity"] == "high"
    assert consistency._same_text("Dues: $700!", "dues $700")


def test_issue_lifecycle(clean, client):
    fake_llm(clean, '{"conflict": true, "topic": "Annual dues", "a_says": "$650", "b_says": "$700", "severity": "high"}')
    clean.setattr(consistency, "candidate_pairs", lambda since=None: [pair()])
    with tx() as conn:   # the check doesn't need real documents; use ids that won't be auto-resolved as removed
        conn.execute("alter table content_issues drop constraint if exists content_issues_a_doc_id_fkey")
        conn.execute("alter table content_issues drop constraint if exists content_issues_b_doc_id_fkey")
    s = consistency.run({"scope": "all", "since": None})
    assert s["new"] == 1 and consistency.open_count()["high"] == 1
    assert consistency.run({"scope": "all", "since": None})["new"] == 0          # same conflict: not duplicated
    iid = client.get("/api/admin/content-issues").json()["issues"][0]["id"]
    assert client.put(f"/api/admin/content-issues/{iid}", json={"status": "dismissed", "note": "not a conflict"}).status_code == 200
    consistency.run({"scope": "all", "since": None})
    assert consistency.open_count()["open"] == 0                                  # dismissed stays dismissed
    # Documents fixed: the pair is rechecked and no longer conflicts -> open issues auto-resolve.
    client.put(f"/api/admin/content-issues/{iid}", json={"status": "open"})
    fake_llm(clean, '{"conflict": false}')
    assert consistency.run({"scope": "changed", "since": None})["auto_resolved"] == 1
    with tx() as conn:
        assert conn.execute("select status from content_issues where id = %s", (iid,)).fetchone()["status"] == "resolved"


def test_scan_request_and_digest(clean, client):
    clean.setattr("cypress.admin.content_checks.start_ingest", lambda: None)
    assert client.post("/api/admin/content-checks/scan").status_code == 202
    assert consistency.request("changed", "job") is None                          # a full scan is already queued
    assert digest.content_blocks({"open": 0}) == []
    txt = digest.content_blocks({"open": 3, "high": 2})[0]["text"]["text"]
    assert "3 content inconsistencies" in txt and "admin/checks" in txt


def _doc(conn, set_id, title, passages):
    d = conn.execute("""insert into documents (set_id, uri, title, content_hash, markdown) values (%s, %s, %s, %s, '')
                        returning id""", (set_id, f"https://example.org/{title}", title, title)).fetchone()["id"]
    vec = str([1.0] + [0.0] * 767)                                   # identical vectors: every pair is "close"
    for i, (section, text) in enumerate(passages):
        conn.execute("insert into chunks (document_id, ord, section_path, content, tokens, embedding) values (%s, %s, %s, %s, 10, %s::vector)",
                     (d, i, section, text, vec))
    return d


def test_pairs_within_a_document_and_rules_without_numbers(clean, client):
    s = client.post("/api/admin/sets", json={"slug": "pairs-test", "name": "pairs-test"}).json()
    with tx() as conn:
        conn.execute("delete from chunks where document_id in (select id from documents where set_id <> %s)", (s["id"],))
        a = _doc(conn, s["id"], "Bylaws", [("Bylaws > Article IV", "Annual dues are $650."),
                                           ("Bylaws > Article IV", "Annual dues are $650. Late after March 1."),   # overlap with ord 0
                                           ("Bylaws > Article VI", "The board sets the agenda."),
                                           ("Bylaws > Schedule A", "Annual dues: $700.")])
        b = _doc(conn, s["id"], "Dock Rules", [("Dock Rules", "The dock is for members only.")])
    base = {"enabled": True, "min_similarity": 0.5, "max_pairs": 300, "neighbors": 10}
    put = lambda **kw: client.put("/api/admin/content-checks/settings", json={**base, **kw}).status_code == 200

    def pairs():
        from cypress import org
        org.clear()
        return {(p["a_title"], p["b_title"]) for p in consistency.candidate_pairs()}

    assert put(same_document=True, rules=True)
    got = pairs()
    assert any("Article IV" in x and "Schedule A" in y or "Schedule A" in x and "Article IV" in y for x, y in got)  # within one document
    assert not any("Article IV" in x and "Article IV" in y for x, y in got)            # neighbouring (overlapping) chunks skipped
    assert any("Dock Rules" in (x, y) for x, y in got)                                 # a rule with no numbers
    assert not any("Article VI" in x + y for x, y in got)                              # no number, no rule: not checked
    assert put(same_document=False, rules=False)
    assert pairs() == set()            # the only numbered passages are in one document, and the dock rule has no number
    put(same_document=True, rules=True, min_similarity=0.78, neighbors=5)


def test_rule_conflicts_pass_the_filter(clean):
    fake_llm(clean, '{"conflict": true, "topic": "Dock access", "a_says": "Guests may use the dock", "b_says": "Members only", "severity": "medium"}')
    assert consistency.check_pair(pair(a="Guests may use the dock.", b="The dock is for members only."))["topic"] == "Dock access"
    fake_llm(clean, '{"conflict": true, "topic": "Dock", "a_says": "Passage A does not mention guests", "b_says": "Members only"}')
    assert consistency.check_pair(pair()) is None


def test_same_value_and_not_applicable_are_not_conflicts(clean):
    fake_llm(clean, '{"conflict": true, "topic": "Age", "a_says": "15 or older", "b_says": "15 or older."}')
    assert consistency.check_pair(pair()) is None
    fake_llm(clean, '{"conflict": true, "topic": "Dues", "a_says": "Not applicable to this passage.", "b_says": "$700 per year"}')
    assert consistency.check_pair(pair()) is None


def test_second_opinion_drops_false_alarms(clean):
    replies = {"consistency": '{"conflict": true, "topic": "Dues", "a_says": "$700 member dues", "b_says": "$600 deeded access", "severity": "high"}',
               "consistency_verify": '{"conflict": false, "reason": "Different fees"}'}
    clean.setattr(models, "generate", lambda node, *a, **k: (replies[node], "m"))
    assert consistency.check_pair(pair()) is None
    replies["consistency_verify"] = '{"conflict": true}'
    assert consistency.check_pair(pair())["topic"] == "Dues"
    fake_llm(clean, '{"conflict": true, "topic": "x", "a_says": "Passage A provides navigation links", "b_says": "$700"}')
    assert consistency.check_pair(pair()) is None
