"""What changed: diffs against the previous version, model summary, digest lines, admin review."""

import json

from cypress import changes, digest, models
from cypress.db import tx
from tests.test_ingest import LOREM, _run_queued, _setup, html, site

SUMMARY = {"trivial": False, "changes": [
    {"what": "Pool closing time", "before": "9 pm", "after": "8 pm", "important": False},
    {"what": "Guest fee", "before": "$5", "after": "$10", "important": True}]}


def test_diff_ignores_whitespace_and_keeps_changed_lines():
    d = changes.diff("Pool\n\nOpen  9 am to 9 pm.\nGuests $5.", "Pool\nOpen 9 am to 8 pm.\nGuests $5.\nNo glass.")
    assert d["removed"] == ["Open 9 am to 9 pm."] and d["added"] == ["Open 9 am to 8 pm.", "No glass."]
    assert changes.diff("a\n  b", "a\nb") == {"removed": [], "added": [], "diff": ""}


def test_recrawl_records_changes(client, monkeypatch):
    calls = []
    monkeypatch.setattr(models, "generate", lambda node, system, msgs, **k: calls.append((node, msgs[0]["content"])) or (json.dumps(SUMMARY), "m"))
    with tx() as conn:
        conn.execute("delete from document_changes")
    s, src = _setup(client, "changes-test")
    pages = {"/": html("Home", f"<p>{LOREM}</p>", ["/pool", "/old"]),
             "/pool": html("Pool Rules", f"<p>Pool closes at 9 pm. Guests $5. {LOREM}</p>"),
             "/old": html("Old Notice", f"<p>Old notice. {LOREM}</p>")}
    http, _ = site(pages)
    client.post(f"/api/admin/sources/{src['id']}/run")
    _run_queued(http)
    assert changes.listing("all")["changes"] == [] and not calls             # first crawl: nothing to compare

    pages["/"] = html("Home", f"<p>{LOREM}</p>", ["/pool", "/new"])
    pages["/pool"] = html("Pool Rules", f"<p>Pool closes at 8 pm. Guests $10. {LOREM}</p>")
    pages["/new"] = html("Kayak Launch", f"<p>New kayak rules. {LOREM}</p>")
    client.post(f"/api/admin/sources/{src['id']}/run")
    _run_queued(http)
    got = {c["kind"]: c for c in client.get("/api/admin/content-changes").json()["changes"]}
    assert set(got) == {"changed", "new", "gone"}
    assert got["changed"]["title"] == "Pool Rules" and got["changed"]["summary"] == SUMMARY["changes"]
    assert "9 pm" in got["changed"]["diff"] and got["new"]["title"] == "Kayak Launch" and got["gone"]["title"] == "Old Notice"
    assert len(calls) == 1 and calls[0][0] == "change_summary" and "Removed lines" in calls[0][1]

    # Reviewing.
    cid = got["changed"]["id"]
    assert client.put(f"/api/admin/content-changes/{cid}", json={"status": "reviewed"}).status_code == 200
    assert client.get("/api/admin/content-changes").json()["unreviewed"] == 2
    assert client.post("/api/admin/content-changes/reviewed").json()["reviewed"] == 2

    # Trivial changes are recorded but pre-reviewed and kept out of the digest; no summary past the limit.
    monkeypatch.setattr(models, "generate", lambda *a, **k: ('{"trivial": true, "changes": []}', "m"))
    pages["/pool"] = html("Pool Rules", f"<p>Pool closes at 8 pm. Guests $10. {LOREM} Updated Nov 2026.</p>")
    client.post(f"/api/admin/sources/{src['id']}/run")
    _run_queued(http)
    latest = client.get("/api/admin/content-changes?status=all").json()["changes"][0]
    assert latest["trivial"] and latest["status"] == "reviewed"


def test_switched_off(client, monkeypatch):
    monkeypatch.setattr(changes, "settings", lambda: {"track_changes": False, "summarize_changes": True, "max_changed_docs": 5})
    assert changes.record_job({"id": 1, "source_id": None, "started_at": "2000-01-01"}) == \
        {"changed": 0, "summarized": 0, "trivial": 0, "new": 0, "gone": 0}


def test_digest_change_lines():
    lines = digest.change_lines([
        {"title": "Pool Rules", "uri": "https://example.org/pool", "kind": "changed", "summary": SUMMARY["changes"]},
        {"title": "Kayak <Launch>", "uri": None, "kind": "new", "summary": None},
        {"title": "Old", "uri": None, "kind": "gone", "summary": None},
        {"title": "Notice", "uri": None, "kind": "changed", "summary": None}])
    assert lines[0] == "• <https://example.org/pool|Pool Rules>: Guest fee: $5 → $10; Pool closing time: 9 pm → 8 pm"
    assert lines[1] == "• New page: Kayak &lt;Launch&gt;" and lines[2].startswith("• No longer") and lines[3] == "• Notice updated"
    r = {"new": 1, "updated": 2, "gone": 1, "failed": 0}
    text = digest.recrawl_blocks(r, [{"title": "Pool Rules", "uri": None, "kind": "changed", "summary": SUMMARY["changes"]}])[0]["text"]["text"]
    assert "Guest fee: $5 → $10" in text and "See what changed" in text
