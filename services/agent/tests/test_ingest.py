import hashlib
import httpx
import pymupdf

from cypress.ingest import worker
from cypress.ingest.chunk import chunk_markdown
from cypress.ingest.fetch import normalize

LOREM = "The park is open to members and their guests from dawn until dusk. " * 6


def site(pages: dict[str, tuple[str, bytes]], etags: bool = False):
    """A fake website for httpx.MockTransport: path -> (content-type, body). With etags, it sends an
    ETag and answers If-None-Match with 304 like a real server."""
    hits: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if req.url.path in pages:
            ct, body = pages[req.url.path]
            tag = '"' + hashlib.md5(body).hexdigest() + '"'
            if etags and req.headers.get("if-none-match") == tag:
                return httpx.Response(304, headers={"etag": tag})
            return httpx.Response(200, headers={"content-type": ct, **({"etag": tag} if etags else {})}, content=body)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True), hits


def html(title: str, body: str, links: list[str] = ()) -> tuple[str, bytes]:
    a = "".join(f'<a href="{h}">link</a> ' for h in links)
    return "text/html", f"<html><head><title>{title}</title></head><body><nav>{a}</nav><article><h1>{title}</h1>{body}</article></body></html>".encode()


def pdf(text: str) -> tuple[str, bytes]:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontsize=11)
    return "application/pdf", doc.tobytes()


def fake_embed(texts):
    return [[1.0] + [0.0] * 767 for _ in texts]


def test_normalize_strips_tracking_and_fragments():
    assert normalize("HTTPS://LakeHillsCA.org/rules?utm_source=x&page=2#top") == "https://lakehillsca.org/rules?page=2"


def test_chunker_keeps_section_paths_and_sizes():
    md = "# Bylaws\n\nIntro.\n\nARTICLE IV. Membership\n\n" + "\n\n".join(["Members must pay dues annually. " * 20] * 6)
    chunks = chunk_markdown(md, "Bylaws", chunk_tokens=300, overlap=50)
    assert chunks[0].section_path == "Bylaws"
    assert all(c.section_path == "Bylaws > ARTICLE IV. Membership" for c in chunks[1:])
    assert len(chunks) > 3 and all(c.tokens <= 300 + 50 for c in chunks)


def _setup(client, slug="ingest-test"):
    s = client.post("/api/admin/sets", json={"slug": slug, "name": slug}).json()
    src = client.post(f"/api/admin/sets/{s['id']}/sources", json={
        "kind": "web", "url": "https://example.org/", "config": {"max_depth": 2, "exclude": ["/skip"]}}).json()
    return s, src


def _run_queued(http):
    while (job := worker.claim()):
        worker.run_job(job, http, fake_embed, ocr=None)


def test_crawl_ingests_dedupes_and_marks_gone(client):
    s, src = _setup(client)
    pages = {
        "/": html("Home", f"<p>{LOREM}</p>", ["/rules", "/skip/me", "/private/x", "https://other.org/", "/docs/agreement.pdf"]),
        "/rules": html("Park Rules", f"<h2>Pool</h2><p>{LOREM}</p><h2>Kayaks</h2><p>{LOREM}</p>"),
        "/docs/agreement.pdf": pdf("Facilities Use and Rental Agreement. " + LOREM),
    }
    http, hits = site(pages)

    client.post(f"/api/admin/sources/{src['id']}/run")
    _run_queued(http)
    job = client.get("/api/admin/jobs", params={"set_id": s["id"]}).json()[0]
    assert job["status"] == "succeeded", job
    assert job["stats"]["new"] == 3 and job["stats"]["chunks"] >= 3
    assert not any("/skip/" in h or "/private/" in h or "other.org" in h for h in hits)  # excludes, robots, domain

    docs = {d["uri"]: d for d in client.get(f"/api/admin/sets/{s['id']}/documents").json()}
    assert set(docs) == {"https://example.org/", "https://example.org/rules", "https://example.org/docs/agreement.pdf"}
    assert docs["https://example.org/docs/agreement.pdf"]["mime"] == "application/pdf"
    rules = client.get(f"/api/admin/documents/{docs['https://example.org/rules']['id']}").json()
    assert "Pool" in rules["markdown"] and rules["title"] == "Park Rules"

    # Second run with nothing changed: identical bytes, so no conversion and no re-embedding.
    client.post(f"/api/admin/sources/{src['id']}/run")
    _run_queued(http)
    job = client.get("/api/admin/jobs", params={"set_id": s["id"]}).json()[0]
    assert job["stats"] == {"pages": 3, "same_bytes": 3}

    # Rules page changes and the PDF link disappears: one update, PDF marked gone.
    pages["/"] = html("Home", f"<p>{LOREM}</p>", ["/rules"])
    pages["/rules"] = html("Park Rules", f"<h2>Pool</h2><p>Pool closes at 9 pm. {LOREM}</p>")
    client.post(f"/api/admin/sources/{src['id']}/run")
    _run_queued(http)
    job = client.get("/api/admin/jobs", params={"set_id": s["id"]}).json()[0]
    assert job["stats"]["updated"] == 1 and job["stats"]["unchanged"] == 1
    docs = {d["uri"]: d for d in client.get(f"/api/admin/sets/{s['id']}/documents").json()}
    assert docs["https://example.org/docs/agreement.pdf"]["status"] == "gone"
    assert docs["https://example.org/rules"]["version"] == 2
    assert len(client.get(f"/api/admin/documents/{docs['https://example.org/rules']['id']}").json()["versions"]) == 2


def test_scanned_pdf_is_ocrd_and_flagged_for_review(client):
    s = client.post("/api/admin/sets", json={"slug": "court", "name": "Court", "visibility": "board"}).json()
    src = client.post(f"/api/admin/sets/{s['id']}/sources",
                      json={"kind": "pdf", "url": "https://example.org/case.pdf"}).json()
    blank = pymupdf.open()
    blank.new_page()  # no text layer = looks scanned
    http, _ = site({"/case.pdf": ("application/pdf", blank.tobytes())})

    client.post(f"/api/admin/sources/{src['id']}/run")
    while (job := worker.claim()):
        worker.run_job(job, http, fake_embed, ocr=lambda png: "# Cause No. 123\n\nThe court finds the easement valid. " * 3)
    job = client.get("/api/admin/jobs", params={"set_id": s["id"]}).json()[0]
    assert job["status"] == "succeeded" and job["stats"]["ocr_pages"] == 1
    doc = client.get(f"/api/admin/sets/{s['id']}/documents").json()[0]
    assert doc["status"] == "needs_review"


def test_failed_fetch_fails_job_with_message(client):
    s = client.post("/api/admin/sets", json={"slug": "broken", "name": "Broken"}).json()
    src = client.post(f"/api/admin/sets/{s['id']}/sources", json={"kind": "pdf", "url": "https://example.org/missing.pdf"}).json()
    http, _ = site({})
    client.post(f"/api/admin/sources/{src['id']}/run")
    _run_queued(http)
    job = client.get("/api/admin/jobs", params={"set_id": s["id"]}).json()[0]
    assert job["status"] == "failed" and "404" in job["error"]


def test_recrawl_uses_304_and_still_follows_links(client):
    s, src = _setup(client, "etag-test")
    pages = {
        "/": html("Home", f"<p>{LOREM}</p>", ["/rules"]),
        "/rules": html("Park Rules", f"<p>{LOREM}</p>", ["/deep"]),
        "/deep": html("Deep Page", f"<p>Deep content. {LOREM}</p>"),
    }
    http, hits = site(pages, etags=True)
    client.post(f"/api/admin/sources/{src['id']}/run")
    _run_queued(http)

    # Nothing changed: every page is a 304, yet the crawl still reaches /deep through stored links,
    # so nothing is marked gone.
    hits.clear()
    client.post(f"/api/admin/sources/{src['id']}/run")
    _run_queued(http)
    job = client.get("/api/admin/jobs", params={"set_id": s["id"]}).json()[0]
    assert job["stats"] == {"pages": 3, "not_modified": 3}, job
    assert any(h.endswith("/deep") for h in hits)
    docs = client.get(f"/api/admin/sets/{s['id']}/documents").json()
    assert {d["status"] for d in docs} == {"active"}

    # One page changes: only it is downloaded in full and re-embedded.
    pages["/deep"] = html("Deep Page", f"<p>New deep content. {LOREM}</p>")
    client.post(f"/api/admin/sources/{src['id']}/run")
    _run_queued(http)
    job = client.get("/api/admin/jobs", params={"set_id": s["id"]}).json()[0]
    assert job["stats"]["not_modified"] == 2 and job["stats"]["updated"] == 1
