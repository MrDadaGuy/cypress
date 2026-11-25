"""Admin API for content management: document sets, sources, jobs, documents.

All routes require the admin role, and every change is written to audit_log in the
same transaction.
"""

from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError, Field, HttpUrl, field_validator

from ..audit import audit
from ..auth import User, require
from ..db import tx
from ..ingest.trigger import start_ingest

router = APIRouter(prefix="/api/admin", tags=["admin: content"])
Admin = Depends(require("admin"))

DocType = Literal["bylaws", "rules", "deed", "court_record", "faq", "history", "newsletter", "minutes", "general"]
Visibility = Literal["public", "members", "staff", "security", "board"]
Schedule = Literal["manual", "daily", "weekly"]


# ---------- models ----------

class SetSettings(BaseModel):
    chunk_tokens: int = Field(800, ge=100, le=4000)
    chunk_overlap: int = Field(100, ge=0, le=1000)
    top_k: int = Field(6, ge=1, le=50)
    min_score: float = Field(0, ge=0, le=1)
    rerank: bool = True


class SetIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field("", max_length=2000)
    visibility: Visibility = "public"
    doc_type: DocType = "general"
    authority_rank: int = Field(50, ge=0, le=100)
    enabled: bool = True
    settings: SetSettings = SetSettings()


class SetPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    visibility: Visibility | None = None
    doc_type: DocType | None = None
    authority_rank: int | None = Field(None, ge=0, le=100)
    enabled: bool | None = None
    settings: SetSettings | None = None


class WebConfig(BaseModel):
    max_depth: int = Field(2, ge=0, le=6)
    max_pages: int = Field(200, ge=1, le=2000)
    allow_domains: list[str] = []      # empty = the seed URL's host only
    include: list[str] = []            # URL path prefixes or globs to keep
    exclude: list[str] = []            # URL path prefixes or globs to skip
    follow_pdfs: bool = True
    respect_robots: bool = True


class FileConfig(BaseModel):
    ocr: Literal["auto", "always", "never"] = "auto"


class SourceIn(BaseModel):
    kind: Literal["web", "pdf"]
    url: HttpUrl
    label: str = Field("", max_length=200)
    config: dict = {}
    schedule: Schedule = "manual"
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def no_credentials(cls, v: HttpUrl) -> HttpUrl:
        if v.username or v.password:
            raise ValueError("URL must not contain credentials")
        return v


class SourcePatch(BaseModel):
    label: str | None = Field(None, max_length=200)
    config: dict | None = None
    schedule: Schedule | None = None
    enabled: bool | None = None


def _config(kind: str, url: str, raw: dict) -> dict:
    """Validate and fill defaults for a source's config."""
    try:
        if kind == "web":
            cfg = WebConfig(**raw)
            if not cfg.allow_domains:
                cfg.allow_domains = [urlparse(url).hostname or ""]
            return cfg.model_dump()
        return FileConfig(**raw).model_dump()
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None


def _one(row, what: str):
    if row is None:
        raise HTTPException(404, f"{what} not found")
    return row


def _update(conn, table: str, id_: int, fields: dict):
    """UPDATE only the provided fields; returns the updated row or None."""
    cols = list(fields)
    sets = ", ".join(f"{c} = %s" for c in cols)  # column names come from pydantic models, not input
    vals = [Jsonb(v) if isinstance(v, dict) else v for v in fields.values()]
    return conn.execute(
        f"update {table} set {sets}, updated_at = now() where id = %s returning *", (*vals, id_)
    ).fetchone()


SET_SUMMARY = """
select s.*,
  (select count(*) from sources where set_id = s.id) as source_count,
  (select count(*) from documents where set_id = s.id and status <> 'gone') as document_count,
  (select count(*) from chunks c join documents d on d.id = c.document_id where d.set_id = s.id) as chunk_count,
  (select row_to_json(j) from (select id, status, created_at, finished_at, error from jobs
     where set_id = s.id order by created_at desc limit 1) j) as last_job
from document_sets s
"""


# ---------- document sets ----------

@router.get("/sets")
def list_sets(user: User = Admin):
    with tx() as conn:
        return conn.execute(SET_SUMMARY + " order by s.name").fetchall()


@router.post("/sets", status_code=201)
def create_set(body: SetIn, user: User = Admin):
    try:
        with tx() as conn:
            row = conn.execute(
                """insert into document_sets
                     (slug, name, description, visibility, doc_type, authority_rank, enabled, settings, created_by)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning *""",
                (body.slug, body.name, body.description, body.visibility, body.doc_type,
                 body.authority_rank, body.enabled, Jsonb(body.settings.model_dump()), user.email),
            ).fetchone()
            audit(conn, user, "create", "document_set", row["id"], body.model_dump())
            return row
    except UniqueViolation:
        raise HTTPException(409, f"A set with slug '{body.slug}' already exists") from None


@router.get("/sets/{set_id}")
def get_set(set_id: int, user: User = Admin):
    with tx() as conn:
        s = _one(conn.execute(SET_SUMMARY + " where s.id = %s", (set_id,)).fetchone(), "Document set")
        s["sources"] = conn.execute(
            """select src.*, (select row_to_json(j) from (select id, status, created_at, finished_at, stats, error
                 from jobs where source_id = src.id order by created_at desc limit 1) j) as last_job
               from sources src where set_id = %s order by created_at""",
            (set_id,),
        ).fetchall()
    from .. import recrawl
    nxt = recrawl.next_due_by_source(set_id)
    for src in s["sources"]:
        src["next_check"] = nxt.get(src["id"])   # None: manual, disabled, or running now
    return s


@router.patch("/sets/{set_id}")
def update_set(set_id: int, body: SetPatch, user: User = Admin):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Nothing to update")
    with tx() as conn:
        row = _one(_update(conn, "document_sets", set_id, fields), "Document set")
        audit(conn, user, "update", "document_set", set_id, fields)
        return row


@router.delete("/sets/{set_id}", status_code=204)
def delete_set(set_id: int, user: User = Admin):
    with tx() as conn:
        row = _one(conn.execute("delete from document_sets where id = %s returning slug, name", (set_id,)).fetchone(),
                   "Document set")
        audit(conn, user, "delete", "document_set", set_id, row)


# ---------- sources ----------

@router.post("/sets/{set_id}/sources", status_code=201)
def create_source(set_id: int, body: SourceIn, user: User = Admin):
    url = str(body.url)
    cfg = _config(body.kind, url, body.config)
    with tx() as conn:
        _one(conn.execute("select 1 from document_sets where id = %s", (set_id,)).fetchone(), "Document set")
        row = conn.execute(
            """insert into sources (set_id, kind, url, label, config, schedule, enabled, created_by)
               values (%s, %s, %s, %s, %s, %s, %s, %s) returning *""",
            (set_id, body.kind, url, body.label, Jsonb(cfg), body.schedule, body.enabled, user.email),
        ).fetchone()
        audit(conn, user, "create", "source", row["id"], {"set_id": set_id, "kind": body.kind, "url": url})
        return row


@router.patch("/sources/{source_id}")
def update_source(source_id: int, body: SourcePatch, user: User = Admin):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Nothing to update")
    with tx() as conn:
        src = _one(conn.execute("select kind, url from sources where id = %s", (source_id,)).fetchone(), "Source")
        if "config" in fields:
            fields["config"] = _config(src["kind"], src["url"] or "", fields["config"])
        row = _update(conn, "sources", source_id, fields)
        audit(conn, user, "update", "source", source_id, fields)
        return row


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: int, user: User = Admin):
    with tx() as conn:
        row = _one(conn.execute("delete from sources where id = %s returning set_id, url", (source_id,)).fetchone(),
                   "Source")
        audit(conn, user, "delete", "source", source_id, row)


# ---------- jobs ----------

def _enqueue(conn, user: User, set_id: int, source_ids: list[int]) -> list[dict]:
    jobs = []
    for sid in source_ids:
        active = conn.execute(
            "select id from jobs where source_id = %s and status in ('queued', 'running')", (sid,)
        ).fetchone()
        if active:
            continue  # don't stack duplicate runs of the same source
        jobs.append(conn.execute(
            "insert into jobs (set_id, source_id, triggered_by) values (%s, %s, %s) returning *",
            (set_id, sid, user.email),
        ).fetchone())
    if jobs:
        audit(conn, user, "run", "document_set", set_id, {"jobs": [j["id"] for j in jobs]})
    return jobs


@router.post("/sets/{set_id}/run")
def run_set(set_id: int, user: User = Admin):
    with tx() as conn:
        _one(conn.execute("select 1 from document_sets where id = %s", (set_id,)).fetchone(), "Document set")
        ids = [r["id"] for r in conn.execute(
            "select id from sources where set_id = %s and enabled and kind <> 'upload'", (set_id,))]
        queued = _enqueue(conn, user, set_id, ids)
    if queued:
        start_ingest()  # after commit, so the worker can see the jobs
    return {"queued": queued}


@router.post("/sources/{source_id}/run")
def run_source(source_id: int, user: User = Admin):
    with tx() as conn:
        src = _one(conn.execute("select set_id from sources where id = %s", (source_id,)).fetchone(), "Source")
        queued = _enqueue(conn, user, src["set_id"], [source_id])
    if queued:
        start_ingest()
    return {"queued": queued}


@router.get("/jobs")
def list_jobs(set_id: int | None = None, limit: int = Query(50, le=200), user: User = Admin):
    with tx() as conn:
        return conn.execute(
            """select j.*, s.url as source_url, s.label as source_label, ds.name as set_name
               from jobs j left join sources s on s.id = j.source_id join document_sets ds on ds.id = j.set_id
               where (%(set)s::bigint is null or j.set_id = %(set)s)
               order by j.created_at desc limit %(limit)s""",
            {"set": set_id, "limit": limit},
        ).fetchall()


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, user: User = Admin):
    with tx() as conn:
        row = conn.execute(
            """update jobs set status = 'cancelled', finished_at = now()
               where id = %s and status in ('queued', 'running') returning *""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(409, "Job is not queued or running")
        audit(conn, user, "cancel", "job", job_id)
        return row


# ---------- documents ----------

@router.get("/sets/{set_id}/documents")
def list_documents(set_id: int, q: str = "", user: User = Admin):
    with tx() as conn:
        return conn.execute(
            """select d.id, d.uri, d.title, d.mime, d.status, d.version, d.fetched_at, d.meta,
                 length(d.markdown) as chars, (select count(*) from chunks where document_id = d.id) as chunks
               from documents d
               where d.set_id = %s and (%s = '' or d.title ilike %s or d.uri ilike %s)
               order by d.title, d.uri""",
            (set_id, q, f"%{q}%", f"%{q}%"),
        ).fetchall()


@router.get("/documents/{doc_id}")
def get_document(doc_id: int, user: User = Admin):
    with tx() as conn:
        d = _one(conn.execute("select * from documents where id = %s", (doc_id,)).fetchone(), "Document")
        d["versions"] = conn.execute(
            "select version, content_hash, fetched_at from document_versions where document_id = %s order by version desc",
            (doc_id,),
        ).fetchall()
        return d


@router.patch("/documents/{doc_id}")
def set_document_status(doc_id: int, status: Literal["active", "excluded"], user: User = Admin):
    """Exclude a document from retrieval (or bring it back) without deleting it."""
    with tx() as conn:
        row = _one(conn.execute(
            "update documents set status = %s, updated_at = now() where id = %s returning id, status",
            (status, doc_id)).fetchone(), "Document")
        audit(conn, user, "set_status", "document", doc_id, {"status": status})
        return row


# ---------- audit ----------

@router.get("/audit")
def audit_log(limit: int = Query(100, le=500), user: User = Admin):
    with tx() as conn:
        return conn.execute("select * from audit_log order by at desc limit %s", (limit,)).fetchall()
