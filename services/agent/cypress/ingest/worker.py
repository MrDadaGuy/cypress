"""Ingest worker: claims queued jobs and runs them.

For each job: fetch (crawl or single PDF) -> Markdown -> compare content hash -> chunk ->
embed -> store. Unchanged documents are skipped at the cheapest point that can tell:
  1. conditional GET (ETag / Last-Modified): server answers 304, nothing is downloaded
  2. same raw bytes as last time: skip conversion and OCR
  3. same Markdown as last time: skip chunking and embedding
A full download is forced every REVALIDATE_DAYS in case a server's validators can't be trusted.

Run locally:      .venv/bin/python -m cypress.ingest.worker          (polls forever)
                  .venv/bin/python -m cypress.ingest.worker --once   (drain the queue, then exit)
On GCP it runs as a Cloud Run Job with --once, triggered when a job is queued and by Scheduler.
"""

import argparse
import hashlib
import logging
import time
from dataclasses import dataclass, field

import httpx
from psycopg.types.json import Jsonb

from ..config import settings
from ..db import migrate, pool, tx
from . import chunk as chunking
from .convert import Converted, OcrFn, gemini_ocr, html_to_md, pdf_to_md
from .embed import EmbedFn, vertex_embedder
from .fetch import Known, Page, client, crawl, fetch

log = logging.getLogger("cypress.ingest")
REVALIDATE_DAYS = 30


@dataclass
class Stats:
    pages: int = 0
    not_modified: int = 0     # 304 from the server
    same_bytes: int = 0       # downloaded, identical file
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    duplicates: int = 0
    failed: int = 0
    gone: int = 0             # pages the site no longer links to
    chunks: int = 0
    ocr_pages: int = 0
    errors: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "errors" and v}


def claim():
    """Take the oldest queued job. SKIP LOCKED lets several workers run safely."""
    with tx() as conn:
        return conn.execute(
            """update jobs set status = 'running', started_at = now()
               where id = (select id from jobs where status = 'queued' order by created_at
                           for update skip locked limit 1)
               returning *"""
        ).fetchone()


def cancelled(job_id: int) -> bool:
    with tx() as conn:
        return conn.execute("select status from jobs where id = %s", (job_id,)).fetchone()["status"] == "cancelled"


def finish(job_id: int, status: str, stats: Stats, error: str | None = None) -> None:
    detail = stats.public() | ({"error_samples": stats.errors[:5]} if stats.errors else {})
    with tx() as conn:
        conn.execute(
            """update jobs set status = %s, finished_at = now(), stats = %s, error = %s
               where id = %s and status = 'running'""",
            (status, Jsonb(detail), error, job_id),
        )


HTTP_COLS = "http_etag = %(etag)s, http_last_modified = %(modified)s, raw_hash = %(raw)s, links = %(links)s, verified_at = now()"


def _http(page: Page) -> dict:
    return {"etag": page.etag, "modified": page.last_modified, "links": page.links,
            "raw": hashlib.sha256(page.body).hexdigest()}


def store(set_row: dict, source_id: int, uri: str, mime: str, doc: Converted, embed: EmbedFn, stats: Stats,
          http: dict | None = None) -> None:
    """Upsert one document; re-chunk and re-embed only if its content changed."""
    digest = hashlib.sha256(doc.markdown.encode()).hexdigest()
    http = http or {"etag": None, "modified": None, "links": None, "raw": None}
    with tx() as conn:
        cur = conn.execute(
            "select id, content_hash, version, status from documents where set_id = %s and uri = %s for update",
            (set_row["id"], uri),
        ).fetchone()
        has_chunks = cur and conn.execute("select 1 from chunks where document_id = %s limit 1", (cur["id"],)).fetchone()
        if cur and cur["content_hash"] == digest and has_chunks and cur["status"] != "gone":
            conn.execute(f"update documents set fetched_at = now(), {HTTP_COLS} where id = %(id)s",
                         {**http, "id": cur["id"]})
            stats.unchanged += 1
            return

    s = set_row["settings"]
    pieces = chunking.chunk_markdown(doc.markdown, doc.title, s.get("chunk_tokens", 800), s.get("chunk_overlap", 100))
    vectors = embed([chunking.embed_text(c, doc.title) for c in pieces]) if pieces else []  # outside the transaction
    status = "needs_review" if doc.needs_review else "active"

    with tx() as conn:
        if cur:
            keep_excluded = cur["status"] == "excluded"
            row = conn.execute(
                f"""update documents set title = %(title)s, mime = %(mime)s, content_hash = %(digest)s,
                     markdown = %(md)s, meta = %(meta)s, version = version + 1, status = %(status)s,
                     source_id = %(src)s, fetched_at = now(), updated_at = now(), {HTTP_COLS}
                   where id = %(id)s returning id, version, content_hash, markdown, fetched_at""",
                {**http, "title": doc.title, "mime": mime, "digest": digest, "md": doc.markdown,
                 "meta": Jsonb(doc.meta), "status": "excluded" if keep_excluded else status,
                 "src": source_id, "id": cur["id"]},
            ).fetchone()
            conn.execute("delete from chunks where document_id = %s", (row["id"],))
            stats.updated += 1
        else:
            row = conn.execute(
                """insert into documents (set_id, source_id, uri, title, mime, status, content_hash, markdown, meta,
                     http_etag, http_last_modified, raw_hash, links, verified_at)
                   values (%(set)s, %(src)s, %(uri)s, %(title)s, %(mime)s, %(status)s, %(digest)s, %(md)s, %(meta)s,
                     %(etag)s, %(modified)s, %(raw)s, %(links)s, now())
                   returning id, version, content_hash, markdown, fetched_at""",
                {**http, "set": set_row["id"], "src": source_id, "uri": uri, "title": doc.title, "mime": mime,
                 "status": status, "digest": digest, "md": doc.markdown, "meta": Jsonb(doc.meta)},
            ).fetchone()
            stats.new += 1
        conn.execute(
            """insert into document_versions (document_id, version, content_hash, markdown, fetched_at)
               values (%s, %s, %s, %s, %s) on conflict do nothing""",
            (row["id"], row["version"], row["content_hash"], row["markdown"], row["fetched_at"]),
        )
        with conn.cursor() as c:
            c.executemany(
                """insert into chunks (document_id, ord, section_path, content, tokens, embedding)
                   values (%s, %s, %s, %s, %s, %s::vector)""",
                [(row["id"], i, p.section_path, p.content, p.tokens, str(v)) for i, (p, v) in enumerate(zip(pieces, vectors))],
            )
        stats.chunks += len(pieces)


def convert(page: Page, ocr_mode: str, ocr: OcrFn | None) -> Converted | None:
    if page.mime == "application/pdf":
        return pdf_to_md(page.body, page.url, ocr_mode, ocr)
    return html_to_md(page.body, page.url)


def run_job(job: dict, http: httpx.Client, embed: EmbedFn, ocr: OcrFn | None) -> None:
    stats = Stats()
    with tx() as conn:
        src = conn.execute("select * from sources where id = %s", (job["source_id"],)).fetchone()
        set_row = conn.execute("select * from document_sets where id = %s", (job["set_id"],)).fetchone()
    if not src:
        return finish(job["id"], "failed", stats, "Source was deleted")

    def on_error(url: str, e: Exception):
        stats.failed += 1
        stats.errors.append(f"{url}: {e}")
        log.warning("fetch failed %s: %s", url, e)

    def stop() -> bool:
        return cancelled(job["id"])

    # What we stored last time, keyed by URI. Only healthy, embedded, recently verified documents
    # get validators; anything else is downloaded in full.
    with tx() as conn:
        prior = {r["uri"]: r for r in conn.execute(
            """select d.id, d.uri, d.mime, d.content_hash, d.raw_hash, d.http_etag, d.http_last_modified, d.links,
                      d.status, d.verified_at > now() - make_interval(days => %s) as fresh,
                      exists (select 1 from chunks c where c.document_id = d.id) as has_chunks
               from documents d where d.set_id = %s""", (REVALIDATE_DAYS, set_row["id"]))}

    def reusable(uri: str):
        r = prior.get(uri)
        return r if r and r["status"] != "gone" and r["has_chunks"] else None

    def known(url: str) -> Known | None:
        r = reusable(url)
        if not r or not r["fresh"] or not (r["http_etag"] or r["http_last_modified"]):
            return None
        return Known(r["mime"], r["http_etag"], r["http_last_modified"], r["links"])

    def touch(r: dict, page: Page | None = None) -> None:
        with tx() as conn:
            if page is None:
                conn.execute("update documents set fetched_at = now() where id = %s", (r["id"],))
            else:
                conn.execute(f"update documents set fetched_at = now(), {HTTP_COLS} where id = %(id)s",
                             {**_http(page), "id": r["id"]})

    try:
        if src["kind"] == "web":
            pages = crawl(http, src["url"], src["config"], on_error, stop, known)
        else:
            page = fetch(http, src["url"], known(src["url"]))
            if page is None or page.mime != "application/pdf":
                raise ValueError(f"{src['url']} did not return a PDF")
            pages = iter([page])

        seen: set[str] = set()
        hashes: set[str] = set()
        for page in pages:
            stats.pages += 1
            r = reusable(page.url)
            if page.not_modified and r:
                stats.not_modified += 1
                touch(r)
                seen.add(page.url)
                hashes.add(r["content_hash"])
                continue
            if r and r["raw_hash"] and r["raw_hash"] == hashlib.sha256(page.body).hexdigest():
                stats.same_bytes += 1
                touch(r, page)
                seen.add(page.url)
                hashes.add(r["content_hash"])
                continue
            try:
                doc = convert(page, src["config"].get("ocr", "auto"), ocr)
            except Exception as e:
                on_error(page.url, e)
                continue
            if doc is None:
                stats.skipped += 1  # nav-only or empty page
                continue
            digest = hashlib.sha256(doc.markdown.encode()).hexdigest()
            if digest in hashes:
                stats.duplicates += 1  # same file under another URL (e.g. ?ver= cache-busters)
                continue
            hashes.add(digest)
            stats.ocr_pages += len(doc.meta.get("ocr_pages", []))
            store(set_row, src["id"], page.url, page.mime, doc, embed, stats, _http(page))
            seen.add(page.url)

        if stop():
            return finish(job["id"], "cancelled", stats)
        if src["kind"] == "web" and stats.pages and not stats.failed:
            # Only after a clean full crawl: pages this source no longer links to are marked gone.
            with tx() as conn:
                stats.gone = conn.execute(
                    """update documents set status = 'gone', updated_at = now()
                       where source_id = %s and status in ('active', 'needs_review') and not (uri = any(%s))""",
                    (src["id"], list(seen)),
                ).rowcount
        if stats.pages == 0:
            return finish(job["id"], "failed", stats, stats.errors[0] if stats.errors else "Nothing was fetched")
        finish(job["id"], "succeeded", stats)
        if stats.updated or stats.new or stats.gone:
            # What changed since the previous version (before -> after), for Content checks and the digest.
            from .. import changes
            try:
                changes.record_job(job)
            except Exception:
                log.exception("recording changes for job %s failed", job["id"])
        # Check changed documents against the rest for conflicting facts (runs after the queue drains).
        from .. import consistency
        consistency.request("changed", f"ingest job {job['id']}", since=job["started_at"])
    except Exception as e:
        log.exception("job %s failed", job["id"])
        finish(job["id"], "failed", stats, f"{type(e).__name__}: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="drain the queue and exit")
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--seed", action="store_true", help="create starter document sets first")
    args = ap.parse_args()
    from .. import tracing
    from ..logs import setup
    setup()
    tracing.setup("cypress-ingest")
    pool.open()
    migrate()
    if args.seed:
        from ..tools.seed import seed
        log.info("seeded: %s", seed())
    embed = vertex_embedder(settings.project)
    ocr = None if args.no_ocr else gemini_ocr(settings.project)
    try:
        _loop(args.once, embed, ocr)
    finally:
        tracing.flush()
        pool.close()


def _loop(once: bool, embed: EmbedFn, ocr: OcrFn | None) -> None:
    errors = 0
    with client() as http:
        while True:
            try:
                more = _step(http, embed, ocr)
                errors = 0
            except Exception:
                # e.g. a migration changed a table under a long-running worker; don't die, back off and retry.
                errors += 1
                log.exception("worker step failed (%s in a row)", errors)
                if once and errors >= 3:
                    raise
                time.sleep(min(60, 3 * errors))
                continue
            if more:
                continue
            if once:
                break
            time.sleep(3)


def _step(http, embed: EmbedFn, ocr: OcrFn | None) -> bool:
    """Run one queued item: an ingest job, else a case file, else a content scan. False if nothing was queued."""
    from .. import cases, consistency, tracing
    job = claim()
    if job:
        log.info("running job %s (source %s)", job["id"], job["source_id"])
        with tracing.span("ingest job", job_id=job["id"], source_id=job["source_id"], triggered_by=job.get("triggered_by")):
            run_job(job, http, embed, ocr)
        return True
    if case := cases.claim():
        log.info("building case file %s (ticket %s)", case["id"], case["ticket_id"])
        with tracing.span("incident case file", case_id=case["id"], ticket_id=case["ticket_id"]):
            cases.run(case)
        return True
    if scan := consistency.claim():
        log.info("running content scan %s (%s)", scan["id"], scan["scope"])
        with tracing.span("content scan", scan_id=scan["id"], scope=scan["scope"]):
            consistency.run_queued(scan)
        return True
    return False


if __name__ == "__main__":
    main()
