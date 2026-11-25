"""Board-only document sets must never reach anonymous callers' retrieval results."""

from cypress import retrieval
from cypress.ingest import worker
from tests.test_ingest import fake_embed, html, site


def test_board_sets_hidden_from_anonymous(client, monkeypatch):
    monkeypatch.setattr(retrieval, "embed_query", lambda q: [1.0] + [0.0] * 767)
    monkeypatch.setattr(retrieval, "_rerank", lambda q, p, k: None)

    for slug, vis, path, text in [
        ("pub-rules", "public", "/rules", "Kayak racks are assigned annually to members."),
        ("board-legal", "board", "/legal", "Kayak rack litigation settlement terms are confidential."),
    ]:
        s = client.post("/api/admin/sets", json={"slug": slug, "name": slug, "visibility": vis}).json()
        src = client.post(f"/api/admin/sets/{s['id']}/sources",
                          json={"kind": "web", "url": f"https://{slug}.example{path}", "config": {"max_depth": 0}}).json()
        http, _ = site({path: html(slug, f"<p>{text} " * 5 + "</p>")})
        client.post(f"/api/admin/sources/{src['id']}/run")
        while (job := worker.claim()):
            worker.run_job(job, http, fake_embed, ocr=None)

    anon, _ = retrieval.search("kayak rack", role=None, k=20)
    board, _ = retrieval.search("kayak rack", role="board", k=20)
    assert {p.set_slug for p in anon} & {"board-legal"} == set()
    assert "pub-rules" in {p.set_slug for p in anon}
    assert {"pub-rules", "board-legal"} <= {p.set_slug for p in board}


def test_visibility_matrix(client, monkeypatch):
    """Each role sees exactly its audiences; security and staff don't see each other's sets."""
    monkeypatch.setattr(retrieval, "embed_query", lambda q: [1.0] + [0.0] * 767)
    monkeypatch.setattr(retrieval, "_rerank", lambda q, p, k: None)
    for vis in ("public", "members", "staff", "security", "board"):
        s = client.post("/api/admin/sets", json={"slug": f"m-{vis}", "name": vis, "visibility": vis}).json()
        src = client.post(f"/api/admin/sets/{s['id']}/sources",
                          json={"kind": "web", "url": f"https://{vis}.example/p", "config": {"max_depth": 0}}).json()
        http, _ = site({"/p": html(vis, f"<p>Gate code procedures for the {vis} audience. " * 5 + "</p>")})
        client.post(f"/api/admin/sources/{src['id']}/run")
        while (job := worker.claim()):
            worker.run_job(job, http, fake_embed, ocr=None)

    def seen(role):
        hits = retrieval.search("gate code", role=role, k=50)[0]
        return {p.set_slug.removeprefix("m-") for p in hits if p.set_slug.startswith("m-")}

    assert seen(None) == {"public"}
    assert seen("bogus-role") == {"public"}
    assert seen("member") == {"public", "members"}
    assert seen("staff") == {"public", "members", "staff"}
    assert seen("security") == {"public", "members", "security"}
    assert seen("board") == seen("admin") == {"public", "members", "staff", "security", "board"}
