"""What changed: after an ingest job, compare each updated document with its previous version.

Every version's text is already kept (document_versions), so this is a line diff plus, when the diff is
real, one small-model call that lists the changed facts as before -> after ("change_summary" step; admins
pick the model and edit the wording). New pages and pages that disappeared are recorded too, but only on
sources that had been crawled before, so a first crawl doesn't flood the list. Cost is capped by a
per-job document limit; documents past it are still listed, with the diff but no summary.
"""

import difflib
import logging
import re

from psycopg.types.json import Jsonb

from . import models
from .db import tx

log = logging.getLogger(__name__)
MAX_SIDE = 6000        # characters of removed/added text sent to the model
MAX_DIFF = 12000       # characters of unified diff kept for admins


def settings() -> dict:
    from .consistency import settings as checks
    s = checks()
    return {"track_changes": s.get("track_changes", True), "summarize_changes": s.get("summarize_changes", True),
            "max_changed_docs": int(s.get("max_changed_docs", 25))}


def _lines(md: str) -> list[str]:
    return [re.sub(r"\s+", " ", ln).strip() for ln in md.splitlines() if ln.strip()]


def diff(old: str, new: str) -> dict:
    """Removed and added lines (whitespace-insensitive), and a capped unified diff."""
    a, b = _lines(old), _lines(new)
    removed, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            removed += a[i1:i2]
        if op in ("replace", "insert"):
            added += b[j1:j2]
    text = "\n".join(difflib.unified_diff(a, b, "before", "after", n=1, lineterm=""))
    return {"removed": removed, "added": added, "diff": text[:MAX_DIFF]}


def summarize(title: str, removed: list[str], added: list[str]) -> dict | None:
    from .agent import prompts
    msg = (f"Document: “{title}”\n\nRemoved lines:\n<<<\n{chr(10).join(removed)[:MAX_SIDE]}\n>>>\n\n"
           f"Added lines:\n<<<\n{chr(10).join(added)[:MAX_SIDE]}\n>>>")
    try:
        text, _ = models.generate("change_summary", prompts.system("change_summary"), [{"role": "user", "content": msg}],
                                  json_mode=True, max_tokens=700, temperature=0)
        j = models.parse_json(text)
    except Exception:
        log.warning("change summary failed for %s", title, exc_info=True)
        return None
    items = []
    for c in (j.get("changes") or [])[:8]:
        if not isinstance(c, dict) or not (c.get("before") or c.get("after")):
            continue
        items.append({"what": str(c.get("what") or "Change")[:80], "before": str(c.get("before") or "")[:300],
                      "after": str(c.get("after") or "")[:300], "important": bool(c.get("important"))})
    return {"trivial": bool(j.get("trivial")) and not items, "changes": items}


PENDING_SQL = """
select d.id, d.title, d.uri, d.version, v.markdown as new_md, p.markdown as old_md
from documents d
join document_versions v on v.document_id = d.id and v.version = d.version
join document_versions p on p.document_id = d.id and p.version = d.version - 1
where d.updated_at >= %(since)s and d.version > 1 and (%(src)s::bigint is null or d.source_id = %(src)s)
  and not exists (select 1 from document_changes c where c.document_id = d.id and c.kind = 'changed' and c.version = d.version)
order by d.updated_at
"""


def record_job(job: dict) -> dict:
    """Record what job `job` (id, source_id, started_at) changed. Returns counts."""
    s = settings()
    out = {"changed": 0, "summarized": 0, "trivial": 0, "new": 0, "gone": 0}
    if not s["track_changes"]:
        return out
    since, src = job["started_at"], job.get("source_id")
    with tx() as conn:
        pending = conn.execute(PENDING_SQL, {"since": since, "src": src}).fetchall()
        crawled_before = src is None or bool(conn.execute(
            "select 1 from documents where source_id = %s and created_at < %s limit 1", (src, since)).fetchone())
    budget = s["max_changed_docs"] if s["summarize_changes"] else 0
    for d in pending:
        dd = diff(d["old_md"], d["new_md"])
        if not dd["removed"] and not dd["added"]:
            continue                                   # whitespace-only change
        summary = None
        if budget > 0:
            budget -= 1
            summary = summarize(d["title"], dd["removed"], dd["added"])
        with tx() as conn:
            conn.execute(
                """insert into document_changes (document_id, job_id, kind, version, title, uri, summary, trivial, added, removed, diff, status)
                   values (%s, %s, 'changed', %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict do nothing""",
                (d["id"], job["id"], d["version"], d["title"], d["uri"],
                 Jsonb(summary["changes"]) if summary else None, bool(summary and summary["trivial"]),
                 len(dd["added"]), len(dd["removed"]), dd["diff"],
                 "reviewed" if summary and summary["trivial"] else "new"))
        out["changed"] += 1
        out["summarized"] += 1 if summary else 0
        out["trivial"] += 1 if summary and summary["trivial"] else 0
    if crawled_before:
        with tx() as conn:
            for kind, cond in (("new", "d.created_at >= %(since)s and d.version = 1"),
                               ("gone", "d.status = 'gone' and d.updated_at >= %(since)s")):
                out[kind] = conn.execute(
                    f"""insert into document_changes (document_id, job_id, kind, version, title, uri)
                        select d.id, %(job)s, '{kind}', d.version, d.title, d.uri from documents d
                        where {cond} and (%(src)s::bigint is null or d.source_id = %(src)s)
                        on conflict do nothing""", {"since": since, "src": src, "job": job["id"]}).rowcount
    if any(out.values()):
        log.info("document changes for job %s", job["id"], extra={"json_fields": {"changes": out}})
    return out


def overnight(limit: int = 6) -> list[dict]:
    """Yesterday's scheduled changes worth a line in the digest: not trivial, important ones first."""
    with tx() as conn:
        return conn.execute(
            """select c.title, c.uri, c.kind, c.summary from document_changes c join jobs j on j.id = c.job_id
               where j.triggered_by = 'schedule' and c.at > now() - interval '1 day' and not c.trivial
               order by (c.summary @> '[{"important": true}]') desc nulls last, c.at limit %s""", (limit,)).fetchall()


def listing(status: str = "new", limit: int = 100) -> dict:
    with tx() as conn:
        rows = conn.execute(
            """select id, at, document_id, kind, version, title, uri, summary, trivial, added, removed, diff, status,
                      reviewed_by, reviewed_at
               from document_changes where (%(s)s = 'all' or status = %(s)s) order by at desc limit %(n)s""",
            {"s": status, "n": limit}).fetchall()
        unread = conn.execute("select count(*)::int as n from document_changes where status = 'new'").fetchone()["n"]
    return {"changes": rows, "unreviewed": unread}
