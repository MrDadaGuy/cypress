def make_set(client, slug="bylaws", **kw):
    r = client.post("/api/admin/sets", json={"slug": slug, "name": kw.pop("name", slug.title()), **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_set_crud_and_audit(client):
    s = make_set(client, "governing-docs", visibility="public", doc_type="bylaws", authority_rank=90)
    assert s["settings"]["chunk_tokens"] == 800

    assert client.post("/api/admin/sets", json={"slug": "governing-docs", "name": "x"}).status_code == 409
    assert client.post("/api/admin/sets", json={"slug": "Bad Slug", "name": "x"}).status_code == 422

    r = client.patch(f"/api/admin/sets/{s['id']}", json={"visibility": "board", "settings": {"top_k": 10}})
    assert r.status_code == 200 and r.json()["visibility"] == "board"
    assert r.json()["settings"]["top_k"] == 10

    listed = {x["slug"]: x for x in client.get("/api/admin/sets").json()}
    assert listed["governing-docs"]["source_count"] == 0

    actions = [(a["action"], a["entity"]) for a in client.get("/api/admin/audit").json()]
    assert ("create", "document_set") in actions and ("update", "document_set") in actions


def test_sources_defaults_and_validation(client):
    s = make_set(client, "website")
    r = client.post(f"/api/admin/sets/{s['id']}/sources",
                    json={"kind": "web", "url": "https://lakehillsca.org/park-hours-and-rules"})
    assert r.status_code == 201, r.text
    src = r.json()
    assert src["config"]["allow_domains"] == ["lakehillsca.org"]
    assert src["config"]["max_depth"] == 2

    bad = client.post(f"/api/admin/sets/{s['id']}/sources", json={"kind": "web", "url": "ftp://x.org"})
    assert bad.status_code == 422
    creds = client.post(f"/api/admin/sets/{s['id']}/sources", json={"kind": "pdf", "url": "https://u:p@x.org/a.pdf"})
    assert creds.status_code == 422
    too_deep = client.post(f"/api/admin/sets/{s['id']}/sources",
                           json={"kind": "web", "url": "https://docs.lakehillsca.org", "config": {"max_depth": 99}})
    assert too_deep.status_code == 422

    pdf = client.post(f"/api/admin/sets/{s['id']}/sources",
                      json={"kind": "pdf", "url": "https://lakehillsca.org/rental.pdf"}).json()
    assert pdf["config"] == {"ocr": "auto"}

    detail = client.get(f"/api/admin/sets/{s['id']}").json()
    assert len(detail["sources"]) == 2


def test_run_queues_once_per_source_and_cancel(client):
    s = make_set(client, "court-records", visibility="board", doc_type="court_record")
    for u in ("https://a.example/one.pdf", "https://a.example/two.pdf"):
        client.post(f"/api/admin/sets/{s['id']}/sources", json={"kind": "pdf", "url": u})

    first = client.post(f"/api/admin/sets/{s['id']}/run").json()["queued"]
    assert len(first) == 2
    assert client.post(f"/api/admin/sets/{s['id']}/run").json()["queued"] == []  # no duplicate runs

    job = first[0]
    assert client.post(f"/api/admin/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/admin/jobs/{job['id']}/cancel").status_code == 409
    jobs = client.get("/api/admin/jobs", params={"set_id": s["id"]}).json()
    assert {j["status"] for j in jobs} == {"queued", "cancelled"}


def test_delete_cascades(client):
    s = make_set(client, "temp")
    client.post(f"/api/admin/sets/{s['id']}/sources", json={"kind": "pdf", "url": "https://x.org/a.pdf"})
    client.post(f"/api/admin/sets/{s['id']}/run")
    assert client.delete(f"/api/admin/sets/{s['id']}").status_code == 204
    assert client.get(f"/api/admin/sets/{s['id']}").status_code == 404
    assert client.get("/api/admin/jobs", params={"set_id": s["id"]}).json() == []


def test_requires_sign_in_outside_dev(client, monkeypatch):
    from cypress import auth
    from cypress.config import Settings

    monkeypatch.setattr(auth, "settings", Settings(env="prod", dev_user="tester@example.com"))
    assert client.get("/api/admin/sets").status_code == 401
    assert client.get("/api/admin/sets", headers={"authorization": "Bearer junk"}).status_code == 401
