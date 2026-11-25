"""Board notifications in Slack (new tickets), with buttons that update the ticket."""

import logging

from . import org, tickets
from .config import settings

log = logging.getLogger(__name__)

EMOJI = {"urgent": "🚨", "high": "🔴", "normal": "🟡", "low": "⚪"}
STATUS = {"new": "New", "acknowledged": "Acknowledged", "in_progress": "In progress",
          "resolved": "Resolved", "closed": "Closed"}


def ticket_blocks(t: dict, footer: str | None = None, photos: int | None = None, cc: list[str] | None = None,
                  owner: str | None = None) -> list[dict]:
    console = f"https://lhca-cypress.web.app/admin/tickets/{t['id']}"
    who = t.get("reporter_role") or "a resident (anonymous)"
    lines = [f"{EMOJI.get(t['priority'], '')} *<{console}|#{t['id']} {t['title']}>*",
             t["description"][:1500]]
    meta = [f"*{STATUS.get(t['status'], t['status'])}*", t["category"], f"priority {t['priority']}",
            f"via {t['channel']}", f"from {who}"]
    if t.get("location"):
        meta.insert(1, f"📍 {t['location']}")
    if t.get("assignee"):
        meta.append(f"assigned to {t['assignee']}")
    n = photos if photos is not None else len(t.get("photos") or [])
    if n:
        meta.append(f"📎 <{console}|{n} attachment{'s' if n > 1 else ''}>")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
              {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(meta)}]}]
    if owner or cc:
        bits = ([owner] if owner else []) + (["cc " + " ".join(f"<@{u}>" for u in cc)] if cc else [])
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(bits)}]})
    if t["status"] in ("new", "acknowledged", "in_progress"):
        buttons = []
        if t["status"] == "new":
            buttons.append({"type": "button", "action_id": "tk_ack", "value": str(t["id"]),
                            "text": {"type": "plain_text", "text": "Acknowledge"}})
        buttons.append({"type": "button", "action_id": "tk_resolve", "value": str(t["id"]), "style": "primary",
                        "text": {"type": "plain_text", "text": "Resolve"}})
        blocks.append({"type": "actions", "block_id": "ticket", "elements": buttons})
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


def new_ticket(t: dict, photos: int = 0) -> bool:
    """Post a new ticket to the board channel. Returns True if posted."""
    if not (settings.slack_bot_token and settings.slack_signing_secret):
        return False
    channel = org.get().get("board_slack_channel") or "#community-association-board"
    try:
        from . import people, routing
        from .slack import bolt
        cc = people.mentions_for_category(t["category"])
        owner = None
        if (s := routing.suggest(t))["people"]:
            who = [f"<@{p['slack_user_id']}>" if p["slack_user_id"] else p["name"] for p in s["people"]]
            owner = f"💡 Suggested owner: {', '.join(who)} ({s['reason']})" if s["reason"] else f"💡 Suggested owner: {', '.join(who)}"
            cc = [u for u in cc if u not in {p["slack_user_id"] for p in s["people"]}]
            try:
                tickets.comment(t["id"], f"Suggested owner: {', '.join(p['name'] for p in s['people'])}. {s['reason']}".strip(),
                                role="admin", actor="cypress")
            except Exception:
                log.warning("couldn't note the suggested owner on ticket %s", t.get("id"), exc_info=True)
        r = bolt().client.chat_postMessage(channel=channel, text=f"New ticket #{t['id']}: {t['title']}",
                                           blocks=ticket_blocks(t, photos=photos, cc=cc, owner=owner), unfurl_links=False)
        tickets.update(t["id"], role="admin", actor="cypress", slack_channel=r["channel"], slack_ts=r["ts"])
        return True
    except Exception:
        log.exception("couldn't post ticket %s to %s", t.get("id"), channel)
        return False


def refresh(t: dict, footer: str | None = None) -> None:
    """Re-render a ticket's board message after it changes."""
    from . import uploads
    t = {**t, "photos": [m["id"] for m in uploads.media_for_ticket(t["id"])]}
    if not (t.get("slack_channel") and t.get("slack_ts") and settings.slack_bot_token):
        return
    try:
        from .slack import bolt
        bolt().client.chat_update(channel=t["slack_channel"], ts=t["slack_ts"],
                                  text=f"Ticket #{t['id']}: {t['title']}", blocks=ticket_blocks(t, footer))
    except Exception:
        log.warning("couldn't update Slack message for ticket %s", t.get("id"), exc_info=True)
