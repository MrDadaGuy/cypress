"""Content checks: find passages that state conflicting facts, across documents or within one.

Cheap by construction: only passages that state something checkable are considered (a number or date, or
a rule: must, may not, only, allowed...), each is paired with its nearest passages (pgvector) from other
documents and from distant parts of the same document, and only close pairs go to the "consistency" step
(a small model; configurable). Results become content_issues for admins to fix at the source.
Runs in the ingest worker (no request time limit): after each ingest job for changed documents, and
on demand ("Scan now") for everything. What changed between versions of one document is changes.py.
"""

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from psycopg.types.json import Jsonb

from . import models, org
from .db import tx

log = logging.getLogger(__name__)
DEFAULTS = {"enabled": True, "min_similarity": 0.78, "max_pairs": 300,
            "neighbors": 5, "same_document": True, "rules": True, "verify": True,
            "track_changes": True, "summarize_changes": True, "max_changed_docs": 25}   # last three: changes.py
# A passage states a rule (checkable without numbers). Postgres regex (case-insensitive) for candidates.
RULE_RE = (r"\m(must|shall|may not|must not|shall not|cannot|can not|prohibited|not permitted|not allowed|allowed|"
           r"permitted|required|only|never|no (pets|glass|guests|smoking|alcohol|parking|swimming|fishing|motor)|limited to|"
           r"eligible|ineligible|restricted to)\M")
CHECKABLE = re.compile(r"\d|\b(yes|no|not|never|only|must|shall|may|allowed|permitted|prohibited|required|cannot|"
                       r"can't|eligible|restricted|limited)\b")
SILENCE = re.compile(r"(does not|doesn't|did not) (mention|state|specify|say|include|list)|not (mentioned|stated|specified|applicable|addressed)|"
                     r"no (explicit|mention)|amount not|\bn/a\b|handled separately|not relevant|without (mentioning|stating|specifying|any mention)|"
                     r"\bpassage [ab]\b|navigation|menu")      # describing a passage instead of quoting it

PAIRS_SQL = """
with live as (
  select c.id, c.document_id, c.ord, c.section_path, c.content, c.embedding, d.title, d.uri, d.updated_at
  from chunks c join documents d on d.id = c.document_id
  where d.status in ('active', 'needs_review') and c.embedding is not null
    and (c.content ~ '[0-9]' or (%(rules)s and c.content ~* %(rule_re)s))
)
select a.id as a_id, b.id as b_id, a.document_id as a_doc, b.document_id as b_doc,
       a.title as a_title, b.title as b_title, a.uri as a_uri, b.uri as b_uri,
       a.section_path as a_section, b.section_path as b_section,
       a.content as a_content, b.content as b_content, 1 - (a.embedding <=> b.embedding) as sim
from live a
cross join lateral (
  -- Other documents, or (optionally) the same document far enough away that chunk overlap isn't the match.
  select l.* from live l
  where l.document_id <> a.document_id or (%(same)s and abs(l.ord - a.ord) > 1)
  order by l.embedding <=> a.embedding limit %(k)s
) b
where (%(since)s::timestamptz is null or a.updated_at >= %(since)s or b.updated_at >= %(since)s)
  and 1 - (a.embedding <=> b.embedding) >= %(min_sim)s
order by sim desc
"""


def settings() -> dict:
    return {**DEFAULTS, **(org.get().get("content_checks") or {})}


def candidate_pairs(since=None) -> list[dict]:
    s = settings()
    with tx() as conn:
        rows = conn.execute(PAIRS_SQL, {"since": since, "min_sim": float(s["min_similarity"]), "k": int(s["neighbors"]),
                                        "same": bool(s["same_document"]), "rules": bool(s["rules"]),
                                        "rule_re": RULE_RE}).fetchall()
    seen, out = set(), []
    for r in rows:
        key = (min(r["a_id"], r["b_id"]), max(r["a_id"], r["b_id"]))
        if key in seen or _same_text(r["a_content"], r["b_content"]):
            continue
        seen.add(key)
        r = dict(r)
        if r["a_doc"] == r["b_doc"]:              # same document: say which part each passage is from
            r["a_title"], r["b_title"] = _part(r["a_title"], r["a_section"]), _part(r["b_title"], r["b_section"])
        out.append(r)
        if len(out) >= int(s["max_pairs"]):
            break
    return out


def _part(title: str, section: str | None) -> str:
    last = (section or "").split(" > ")[-1].strip()
    return f"{title} › {last}" if last and last != title else title


def _same_text(a: str, b: str) -> bool:
    """Copies of the same text (e.g. a form on the website and as a file) can't conflict."""
    norm = lambda t: re.sub(r"\W+", " ", t.lower()).strip()
    return norm(a) == norm(b)


def check_pair(p: dict) -> dict | None:
    from .agent import prompts
    same = "Both passages are from the same document, in different sections.\n\n" if p["a_doc"] == p["b_doc"] else ""
    msg = (f"{same}Passage A, from “{p['a_title']}”:\n<<<\n{p['a_content'][:2500]}\n>>>\n\n"
           f"Passage B, from “{p['b_title']}”:\n<<<\n{p['b_content'][:2500]}\n>>>")
    try:
        text, _ = models.generate("consistency", prompts.system("consistency"), [{"role": "user", "content": msg}],
                                  json_mode=True, max_tokens=300, temperature=0)
        j = models.parse_json(text)
    except Exception:
        log.warning("consistency check failed for chunks %s/%s", p["a_id"], p["b_id"], exc_info=True)
        return None
    if not j.get("conflict"):
        return None
    says = f"{j.get('a_says', '')} {j.get('b_says', '')}".lower()
    if SILENCE.search(says) or not CHECKABLE.search(says):
        return None                              # "A doesn't mention it" isn't a conflict
    if _same_text(str(j.get("a_says", "")), str(j.get("b_says", ""))):
        return None                              # both say the same thing
    if settings().get("verify") and not verify(msg, j):
        return None
    return {"topic": str(j.get("topic") or "Conflicting facts")[:80],
            "a_says": str(j.get("a_says") or "")[:400], "b_says": str(j.get("b_says") or "")[:400],
            "severity": j.get("severity") if j.get("severity") in ("high", "medium", "low") else "medium",
            "suggestion": str(j.get("suggestion") or "")[:300] or None}


def verify(msg: str, j: dict) -> bool:
    """Second opinion from a stronger model on a flagged pair. Errors keep the flag (fail open: an admin decides)."""
    from .agent import prompts
    flag = f"\n\nFlagged as: {j.get('topic')}. A says: {j.get('a_says')}. B says: {j.get('b_says')}."
    try:
        text, _ = models.generate("consistency_verify", prompts.system("consistency_verify"),
                                  [{"role": "user", "content": msg + flag}], json_mode=True, max_tokens=200, temperature=0)
        return bool(models.parse_json(text).get("conflict"))
    except Exception:
        log.warning("consistency verify failed", exc_info=True)
        return True


def _fingerprint(a_doc: int, b_doc: int, topic: str) -> str:
    lo, hi = sorted((a_doc, b_doc))
    t = re.sub(r"[^a-z0-9]+", " ", topic.lower()).strip()
    return hashlib.sha256(f"{lo}:{hi}:{t}".encode()).hexdigest()[:32]


def run(scan: dict) -> dict:
    """Scan ('all', or documents changed since scan['since']) and record issues. Returns stats."""
    pairs = candidate_pairs(scan["since"] if scan["scope"] == "changed" else None)
    stats = {"pairs_checked": len(pairs), "conflicts": 0, "new": 0, "auto_resolved": 0}
    with ThreadPoolExecutor(max_workers=4) as pool:
        found = list(pool.map(lambda p: (p, check_pair(p)), pairs))
    seen_fps = set()
    touched_docs = {p["a_doc"] for p in pairs} | {p["b_doc"] for p in pairs}
    with tx() as conn:
        for p, issue in found:
            if not issue:
                continue
            stats["conflicts"] += 1
            fp = _fingerprint(p["a_doc"], p["b_doc"], issue["topic"])
            seen_fps.add(fp)
            r = conn.execute(
                """insert into content_issues (fingerprint, topic, severity, a_doc_id, b_doc_id, a_title, b_title, a_uri, b_uri,
                     a_says, b_says, a_excerpt, b_excerpt, suggestion)
                   values (%(fp)s, %(topic)s, %(severity)s, %(a_doc)s, %(b_doc)s, %(a_title)s, %(b_title)s, %(a_uri)s, %(b_uri)s,
                     %(a_says)s, %(b_says)s, %(a_ex)s, %(b_ex)s, %(suggestion)s)
                   on conflict (fingerprint) do update set last_seen_at = now(), a_says = excluded.a_says,
                     b_says = excluded.b_says, a_excerpt = excluded.a_excerpt, b_excerpt = excluded.b_excerpt,
                     status = case when content_issues.status = 'resolved' then 'open' else content_issues.status end
                   returning (xmax = 0) as inserted""",
                {**issue, "fp": fp, "a_doc": p["a_doc"], "b_doc": p["b_doc"], "a_title": p["a_title"], "b_title": p["b_title"],
                 "a_uri": p["a_uri"], "b_uri": p["b_uri"], "a_ex": _excerpt(p["a_content"]), "b_ex": _excerpt(p["b_content"])}).fetchone()
            stats["new"] += 1 if r["inserted"] else 0
        # Open issues between rescanned documents that weren't found again: the documents now agree.
        if touched_docs:
            stats["auto_resolved"] = conn.execute(
                """update content_issues set status = 'resolved', closed_by = 'content check', closed_at = now(),
                     note = 'No longer found after the documents changed'
                   where status = 'open' and a_doc_id = any(%s) and b_doc_id = any(%s) and not (fingerprint = any(%s))""",
                (list(touched_docs), list(touched_docs), list(seen_fps))).rowcount
        # Issues whose document was removed or excluded.
        stats["auto_resolved"] += conn.execute(
            """update content_issues i set status = 'resolved', closed_by = 'content check', closed_at = now(),
                 note = 'A document was removed'
               where status = 'open' and exists (select 1 from documents d where d.id in (i.a_doc_id, i.b_doc_id)
                                                 and d.status not in ('active', 'needs_review'))""").rowcount
    return stats


def _excerpt(text: str, n: int = 600) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


# ---------------- queue (runs in the ingest worker) ----------------

def request(scope: str, requested_by: str, since=None) -> int | None:
    if not settings().get("enabled"):
        return None
    with tx() as conn:
        if conn.execute("select 1 from content_scans where status = 'queued' and scope = 'all'").fetchone():
            return None                         # a full scan is already waiting; it covers this
        return conn.execute("insert into content_scans (scope, since, requested_by) values (%s, %s, %s) returning id",
                            (scope, since, requested_by)).fetchone()["id"]


def claim() -> dict | None:
    with tx() as conn:
        return conn.execute(
            """update content_scans set status = 'running', started_at = now()
               where id = (select id from content_scans where status = 'queued' order by created_at
                           for update skip locked limit 1) returning *""").fetchone()


def run_queued(scan: dict) -> None:
    try:
        stats = run(scan)
        status, error = "done", None
    except Exception as e:
        log.exception("content scan %s failed", scan["id"])
        stats, status, error = {}, "failed", f"{type(e).__name__}: {e}"[:300]
    with tx() as conn:
        conn.execute("update content_scans set status = %s, finished_at = now(), stats = %s, error = %s where id = %s",
                     (status, Jsonb(stats), error, scan["id"]))
    log.info("content scan %s %s: %s", scan["id"], status, stats)


def open_count() -> dict:
    with tx() as conn:
        return conn.execute("""select count(*) as open, count(*) filter (where severity = 'high') as high
                               from content_issues where status = 'open'""").fetchone()
