"""Morning digest of tickets for the board channel (Cloud Scheduler -> POST /api/internal/digest)."""

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from . import notify, org, tickets
from .auth import require_internal
from .config import settings

log = logging.getLogger(__name__)
router = APIRouter(tags=["internal"])
ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
CONSOLE = "https://lhca-cypress.web.app/admin/tickets"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def terms_blocks(report: dict) -> list[dict]:
    """Board terms ending within 60 days, and admins whose term has ended."""
    lines = [f"🗓️ {r['name']} ({r['title']}): term ends {r['end_date']:%b %-d, %Y}" for r in report.get("ending", [])]
    lines += [f"🔑 {n}'s board term has ended but they still have admin access. Review in Users & roles."
              for n in report.get("lapsed_admins", [])]
    return [{"type": "section", "text": {"type": "mrkdwn", "text": "*Board terms*\n" + "\n".join(lines)}}] if lines else []


def content_blocks(c: dict | None) -> list[dict]:
    """Documents that disagree (content checks): a count and a link, so someone fixes the source."""
    if not c or not c.get("open"):
        return []
    n, high = c["open"], c.get("high", 0)
    text = (f"📄 *{n} content inconsistenc{'y' if n == 1 else 'ies'}* to review"
            f"{f' ({high} about fees, eligibility or rules)' if high else ''}: documents that disagree with each other. "
            f"<https://lhca-cypress.web.app/admin/checks|Review in Content checks>")
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def change_lines(changes: list[dict]) -> list[str]:
    """One line per changed page: its most important fact changes, as before → after."""
    out = []
    for c in changes:
        name = f"<{c['uri']}|{_esc(c['title'] or 'Untitled')}>" if c.get("uri") else _esc(c["title"] or "Untitled")
        if c["kind"] == "new":
            out.append(f"• New page: {name}")
        elif c["kind"] == "gone":
            out.append(f"• No longer on the site: {name}")
        else:
            facts = sorted(c.get("summary") or [], key=lambda f: not f.get("important"))[:2]
            bits = [f"{_esc(f['what'])}: {_esc(f['before']) or '(new)'} → {_esc(f['after']) or '(removed)'}" for f in facts]
            out.append(f"• {name}" + (f": {'; '.join(bits)}" if bits else " updated"))
    return out


def recrawl_blocks(r: dict | None, changes: list[dict] | None = None) -> list[dict]:
    """Overnight website/PDF re-checks: only worth a line when something changed or failed."""
    if not r or not (r["new"] or r["updated"] or r["gone"] or r["failed"]):
        return []
    lines = change_lines(changes or [])
    parts = []
    changed = [f"{r[k]} {label}" for k, label in (("new", "new"), ("updated", "updated"), ("gone", "removed")) if r[k]]
    if changed:
        parts.append(f"pages {', '.join(changed)}. Cypress now answers from the latest versions.")
    if r["failed"]:
        parts.append(f"{r['failed']} source{' was' if r['failed'] == 1 else 's were'} unreachable and will be retried tonight.")
    text = f"🔄 *Overnight re-check:* {' '.join(parts)}"
    if lines:
        text += "\n" + "\n".join(lines) + "\n<https://lhca-cypress.web.app/admin/checks?tab=changes|See what changed>"
    else:
        text += " <https://lhca-cypress.web.app/admin/sets|Review content>"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def retention_blocks(r: dict | None) -> list[dict]:
    """Overnight cleanup: counts only, and only when something was removed or couldn't be."""
    if not r or not (r["photos"] or r["videos"] or r["unattached"] or r["lookups"] or r["failed"]):
        return []
    parts = []
    if r["photos"] or r["videos"]:
        what = " and ".join(_n(r[k], k[:-1]) for k in ("photos", "videos") if r[k])
        parts.append(f"removed {what} from long-closed tickets")
    if r["unattached"]:
        parts.append(f"{_n(r['unattached'], 'upload')} never attached to a report")
    if r["lookups"]:
        parts.append(f"{r['lookups']} old member lookup log entr{'y' if r['lookups'] == 1 else 'ies'}")
    text = "🧹 *Overnight cleanup:* " + (", ".join(parts) + "." if parts else "")
    if r["failed"]:
        text += f" {_n(r['failed'], 'file')} couldn't be removed and will be retried tonight."
    text += " <https://lhca-cypress.web.app/admin/org|Retention settings>"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def sync_blocks(r: dict | None) -> list[dict]:
    """The overnight member-list sync, only when it failed or needs a look (counts only, never names)."""
    if not r or r.get("status") not in ("failed", "warning"):
        return []
    if r["status"] == "failed":
        text = f"⚠️ *Member list sync failed:* {r.get('error') or 'unknown error'}"
    else:
        st = r.get("stats") or {}
        alerts = [c["what"] for c in st.get("changes") or [] if c.get("level") == "alert"]
        items = (st.get("warnings") or []) + alerts
        text = "⚠️ *The membership workbook changed:* " + " ".join(items[:3]) + (f" …and {len(items) - 3} more." if len(items) > 3 else "")
    text += " <https://lhca-cypress.web.app/admin/members|Members>"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def build(rows: list[dict], now: datetime | None = None, terms: dict | None = None,
          content: dict | None = None, recrawl: dict | None = None, retention: dict | None = None,
          changes: list[dict] | None = None, sync: dict | None = None) -> list[dict] | None:
    """Slack blocks for the digest, or None when there's nothing worth posting."""
    now = now or datetime.now(UTC)
    extra = (terms_blocks(terms or {}) + content_blocks(content) + recrawl_blocks(recrawl, changes)
             + retention_blocks(retention) + sync_blocks(sync))
    open_ = sorted([t for t in rows if t["status"] in ("new", "acknowledged", "in_progress")],
                   key=lambda t: (ORDER.get(t["priority"], 9), t["created_at"]))
    stale = [t for t in open_ if t["status"] == "new" and now - _ts(t["created_at"]) > timedelta(hours=24)]
    resolved = [t for t in rows if t["status"] in ("resolved", "closed") and now - _ts(t["updated_at"]) < timedelta(hours=24)]
    if not open_ and not resolved:
        return extra or None

    def line(t):
        age = (now - _ts(t["created_at"])).days
        bits = [f"{notify.EMOJI.get(t['priority'], '')} <{CONSOLE}/{t['id']}|#{t['id']} {t['title']}>",
                notify.STATUS.get(t["status"], t["status"])]
        if t.get("assignee"):
            bits.append(t["assignee"])
        bits.append("today" if age == 0 else f"{age}d old")
        return " · ".join(bits)

    head = f"*Good morning! {len(open_)} open ticket{'s' if len(open_) != 1 else ''}*"
    if stale:
        head += f", {len(stale)} not yet acknowledged after a day"
    if resolved:
        head += f". {len(resolved)} resolved in the last 24 hours 🎉"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    if open_:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(line(t) for t in open_[:15])}})
        if len(open_) > 15:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"…and {len(open_) - 15} more in the <{CONSOLE}|console>"}]})
    return blocks + extra


def post() -> dict:
    from . import people
    rows = tickets.list_tickets(role="admin", actor="digest", status="all", limit=200)
    from . import consistency
    from . import changes, recrawl, retention
    from . import sheet_sync
    last = next((r for r in sheet_sync.recent(3) if r["trigger"] == "nightly"), None)
    blocks = build(rows, terms=people.terms_report(), content=consistency.open_count(), recrawl=recrawl.overnight(),
                   retention=retention.overnight(), changes=changes.overnight(), sync=last)
    if blocks is None:
        return {"posted": False, "reason": "nothing open or resolved"}
    if not settings.slack_bot_token:
        return {"posted": False, "reason": "slack not configured"}
    from .slack import bolt
    channel = org.get().get("board_slack_channel") or "#community-association-board"
    bolt().client.chat_postMessage(channel=channel, text="Daily ticket digest", blocks=blocks, unfurl_links=False)
    return {"posted": True}


@router.post("/api/internal/digest")
async def digest(request: Request):
    require_internal(request)
    return await run_in_threadpool(post)
