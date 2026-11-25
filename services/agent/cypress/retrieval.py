"""Hybrid retrieval: pgvector + Postgres full-text, fused with reciprocal rank fusion, then
reranked by the Vertex AI Ranking API.

Access control lives in the SQL: anonymous callers only ever see 'public' sets. The model
never sees a passage the caller isn't allowed to read.
"""

import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass

from . import roles
from .config import settings
from .db import tx
from .ingest.embed import normalize

log = logging.getLogger(__name__)
CANDIDATES = 30
RRF_K = 60


@dataclass
class Passage:
    chunk_id: int
    document_id: int
    title: str
    uri: str
    section: str
    set_slug: str
    set_name: str
    doc_type: str
    authority: int
    content: str
    score: float = 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("content")
        return d


def visibilities(role: str | None) -> list[str]:
    return roles.visible_to(role)


def embed_query(text: str) -> list[float]:
    from . import models
    return normalize(models.embed_texts([text], node="embed_query", task="RETRIEVAL_QUERY")[0])


SEARCH_SQL = """
with eligible as (
  select c.id, c.document_id, c.content, c.section_path, c.embedding, c.tsv,
         d.title, d.uri, ds.slug, ds.name, ds.doc_type, ds.authority_rank
  from chunks c
  join documents d on d.id = c.document_id and d.status = 'active'
  join document_sets ds on ds.id = d.set_id and ds.enabled and ds.visibility = any(%(vis)s)
),
vec as (
  select id, row_number() over (order by embedding <=> %(qv)s::vector) as r
  from eligible where embedding is not null
  order by embedding <=> %(qv)s::vector limit %(n)s
),
fts as (
  select id, row_number() over (order by ts_rank_cd(tsv, q) desc) as r
  -- Any query word may match (OR), ranked by how many match and how close together. websearch_to_tsquery
  -- ANDs every word, so a natural-language question ("a boat trailer was left in the lot overnight") matched
  -- nothing and keyword search silently dropped out of the fusion.
  from eligible, to_tsquery('simple', coalesce(nullif(array_to_string(array(
         select quote_literal(l) from unnest(tsvector_to_array(to_tsvector('english', %(qt)s))) l), ' | '), ''), '')) q
  where tsv @@ q
  order by ts_rank_cd(tsv, q) desc limit %(n)s
)
select e.*, coalesce(1.0 / (%(k)s + vec.r), 0) + coalesce(1.0 / (%(k)s + fts.r), 0) as rrf
from eligible e
left join vec on vec.id = e.id
left join fts on fts.id = e.id
where vec.id is not null or fts.id is not null
order by rrf desc
limit %(n)s
"""


def _rerank(query: str, passages: list[Passage], top_n: int) -> list[Passage] | None:
    """Rerank with the enabled reranker from the catalog (metered). None if switched off or failing,
    in which case the hybrid-search order is used."""
    from . import finops, models
    m = models.rerank_model()
    if m is None:
        return None
    started = time.monotonic()
    try:
        scores = (_local_scores if m.provider == "local" else _vertex_scores)(m, query, passages)
    except Exception as e:
        finops.record(m, "rerank", "primary", finops.Usage(requests=0), int((time.monotonic() - started) * 1000),
                      ok=False, error=f"{type(e).__name__}: {e}")
        log.warning("rerank unavailable, using fused order: %s", e)
        return None
    finops.record(m, "rerank", "primary", finops.Usage(requests=1), int((time.monotonic() - started) * 1000))
    for i, sc in scores.items():
        passages[i].score = sc
    return sorted((passages[i] for i in scores), key=lambda p: p.score, reverse=True)[:top_n]


def _vertex_scores(m, query: str, passages: list[Passage]) -> dict[int, float]:
    """Vertex AI Ranking API. Rejected requests raise (and aren't billed)."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    url = (f"https://discoveryengine.googleapis.com/v1/projects/{settings.project}"
           "/locations/global/rankingConfigs/default_ranking_config:rank")
    body = {"model": m.name, "query": query, "topN": len(passages),
            "records": [{"id": str(i), "title": f"{p.title} — {p.section}", "content": p.content[:4000]}
                        for i, p in enumerate(passages)]}
    r = AuthorizedSession(creds).post(url, json=body, timeout=10, headers={"x-goog-user-project": settings.project})
    r.raise_for_status()
    return {int(rec["id"]): float(rec.get("score", 0)) for rec in r.json().get("records", [])}


# Local cross-encoder. Small batches with the memory arena off keep it to ~110 MB on the 512 MB API.
FLASHRANK_CACHE = os.getenv("FLASHRANK_CACHE", "/opt/flashrank")
LOCAL_MAX_TOKENS = 256
LOCAL_BATCH = 10
_rankers: dict = {}
_ranker_lock = threading.Lock()


def _ranker(name: str):
    with _ranker_lock:
        if name not in _rankers:
            import onnxruntime as ort
            from flashrank import Ranker
            cache = FLASHRANK_CACHE if os.path.isdir(FLASHRANK_CACHE) else os.path.join(tempfile.gettempdir(), "flashrank")
            rk = Ranker(model_name=name, max_length=LOCAL_MAX_TOKENS, cache_dir=cache, log_level="WARNING")
            so = ort.SessionOptions()
            so.enable_cpu_mem_arena = False
            so.intra_op_num_threads = so.inter_op_num_threads = 1
            rk.session = ort.InferenceSession(rk.session._model_path, so, providers=["CPUExecutionProvider"])
            _rankers[name] = (rk, threading.Lock())
        return _rankers[name]


def _local_scores(m, query: str, passages: list[Passage]) -> dict[int, float]:
    from flashrank import RerankRequest
    rk, lock = _ranker(m.name)
    texts = [{"id": i, "text": f"{p.title} — {p.section}\n{p.content[:2000]}"} for i, p in enumerate(passages)]
    out: dict[int, float] = {}
    with lock:   # one inference at a time keeps memory flat
        for j in range(0, len(texts), LOCAL_BATCH):
            for r in rk.rerank(RerankRequest(query=query, passages=texts[j:j + LOCAL_BATCH])):
                out[int(r["id"])] = float(r["score"])
    return out


def search(query: str, role: str | None, k: int = 6) -> tuple[list[Passage], dict]:
    qv = embed_query(query)
    with tx() as conn:
        rows = conn.execute(SEARCH_SQL, {"vis": visibilities(role), "qv": str(qv), "qt": query,
                                         "n": CANDIDATES, "k": RRF_K}).fetchall()
    cands = [Passage(r["id"], r["document_id"], r["title"], r["uri"], r["section_path"], r["slug"], r["name"],
                     r["doc_type"], r["authority_rank"], r["content"], float(r["rrf"])) for r in rows]
    info = {"query": query, "candidates": len(cands), "reranked": False}
    if not cands:
        return [], info
    ranked = _rerank(query, cands, k)
    if ranked is not None:
        info["reranked"] = True
        return ranked[:k], info
    return cands[:k], info
