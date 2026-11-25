"""Cypress in Slack.

Production (Cloud Run, HTTP Events API):
  Slack -> POST /api/slack/events   verify signature, enqueue a Cloud Task, ack within 3 s
  Cloud Tasks -> POST /api/slack/work   (OIDC-signed) run the agent, post the reply

Cloud Run only gives CPU while a request is open, so the slow part (the agent, ~6 s) runs in its
own request from Cloud Tasks instead of a background thread after the ack. The task name is the
Slack event id, so Slack's retries are de-duplicated for free.

Who is asking: Slack guests and bots get public access. Full workspace members get the role of
their entry in the people table (matched by Slack user id, or by email and then linked), otherwise
the workspace default (org setting slack_default_role).
"""

import base64
import json
import logging
import re
import threading
import time
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool

from . import roles
from .agent import service
from .config import settings

log = logging.getLogger(__name__)
router = APIRouter(tags=["slack"])

# ---------------- formatting ----------------

def to_slack(answer: str, citations: list[dict]) -> str:
    """Markdown -> Slack mrkdwn, with numbered sources linked underneath."""
    text = re.sub(r"\[([^\]\n]+)\]\(((?:https?://|mailto:)[^)\s]+)\)", lambda m: f"<{m.group(2)}|{m.group(1)}>", answer)
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: f"*{m.group(1)}*", text)
    text = re.sub(r"^#+\s*(.+)$", lambda m: f"*{m.group(1)}*", text, flags=re.M)
    text = re.sub(r"^(\s*)[*-] ", lambda m: f"{m.group(1)}• ", text, flags=re.M)
    if citations:
        text += "\n\n" + "\n".join(f"[{c['n']}] <{c['uri']}|{c['title']}>" for c in citations)
    return text


# ---------------- Slack client and identity ----------------

@lru_cache
def bolt():
    from slack_bolt import App
    app = App(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret,
              process_before_response=True)  # listeners only enqueue, so ack after them is still fast
    app.event("app_mention")(_on_mention)
    app.event("message")(_on_message)
    app.action("fb_up")(_on_thumb)
    app.action("fb_down")(_on_thumb)
    app.view("fb_comment")(_on_comment)
    app.action("tk_ack")(_on_ticket_button)
    app.action("tk_resolve")(_on_ticket_button)
    app.action("cv_yes")(_on_vote_button)
    app.action("cv_no")(_on_vote_button)
    app.action("cv_view")(lambda ack: ack())
    return app


@lru_cache
def bot_user_id() -> str:
    return bolt().client.auth_test()["user_id"]


_profiles: dict[str, tuple[float, dict]] = {}   # users.info, cached 10 min (Slack rate limits)


def role_for(user_id: str) -> str | None:
    """Slack user -> Cypress role."""
    from . import people
    hit = _profiles.get(user_id)
    if hit and time.time() - hit[0] < 600:
        u = hit[1]
    else:
        try:
            u = bolt().client.users_info(user=user_id)["user"]
        except Exception:
            log.exception("users.info failed for %s; using public access", user_id)
            return None
        _profiles[user_id] = (time.time(), u)
    if u.get("is_bot") or u.get("is_restricted") or u.get("is_ultra_restricted") or u.get("deleted"):
        return None
    return people.slack_role(user_id, u.get("profile", {}).get("email"))


_members: dict[str, tuple[float, list[str]]] = {}   # conversations.members, cached 10 min
CHANNEL_TTL = 600


def channel_members(channel: str) -> list[str] | None:
    """Everyone in a channel (None if Slack won't say, e.g. a public channel without channels:read)."""
    hit = _members.get(channel)
    if hit and time.time() - hit[0] < CHANNEL_TTL:
        return hit[1]
    try:
        ids, cursor = [], None
        while True:
            r = bolt().client.conversations_members(channel=channel, limit=200, cursor=cursor)
            ids += r["members"]
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception:
        log.warning("couldn't list members of %s; answering at public level", channel, exc_info=True)
        return None
    _members[channel] = (time.time(), ids)
    return ids


def member_role(user_id: str) -> str | None:
    """Role of a channel member for audience purposes: listed people only (no workspace default)."""
    from . import people
    hit = _profiles.get(user_id)
    if hit and time.time() - hit[0] < 600:
        u = hit[1]
    else:
        try:
            u = bolt().client.users_info(user=user_id)["user"]
        except Exception:
            return None
        _profiles[user_id] = (time.time(), u)
    if u.get("is_bot") or u.get("is_restricted") or u.get("is_ultra_restricted") or u.get("deleted"):
        return None
    return people.slack_role(user_id, u.get("profile", {}).get("email"), use_default=False)


def channel_ceiling(channel: str) -> str | None | bool:
    """Admin-set ceiling for a channel: a role, None for public, or False when there's none."""
    from .db import tx
    try:
        with tx() as conn:
            r = conn.execute("select max_role from slack_channels where channel_id = %s", (channel,)).fetchone()
    except Exception:
        log.exception("reading slack channel ceiling failed; answering at public level")
        return None
    if not r or not r["max_role"]:
        return False
    return None if r["max_role"] == "public" else r["max_role"]


def audience(event: dict, user: str) -> tuple[str | None, dict]:
    """The role an answer may use, given who will see it.
    DM: the asker's role. Channel: only what every member may see (listed roles; unlisted people,
    guests and other bots count as public), capped by the channel's admin ceiling. Fails closed."""
    asker = role_for(user)
    if event.get("channel_type") == "im":
        return asker, {"audience": "dm"}
    members = channel_members(event["channel"])
    if members is None:
        return None, {"audience": "unknown"}
    me = bot_user_id()
    rs = [asker] + [member_role(m) for m in members if m not in (me, user)]
    role = roles.common_role(rs)
    ceiling = channel_ceiling(event["channel"])
    if ceiling is not False:
        role = roles.common_role([role, ceiling])
    return role, {"audience": "channel", "members": len(members), "ceiling": ceiling if ceiling is not False else "none"}


# ---------------- answering ----------------

THINKING = "_Cypress is thinking…_ :hourglass_flowing_sand:"


def answer(event: dict) -> None:
    """Run the agent for one Slack message and post the reply (inline, or in the asker's thread).
    If a "thinking" placeholder was posted (event["placeholder_ts"]), the answer replaces it."""
    client = bolt().client
    thread_ts = event.get("thread_ts")
    placeholder = event.get("placeholder_ts")

    def post(text: str, blks: list | None = None) -> None:
        kw = {"text": text, "blocks": blks or blocks(text, None), "unfurl_links": False, "unfurl_media": False}
        if placeholder:
            try:
                client.chat_update(channel=event["channel"], ts=placeholder, **kw)
                return
            except Exception:
                log.warning("couldn't replace placeholder; posting a new message", exc_info=True)
        client.chat_postMessage(channel=event["channel"], thread_ts=thread_ts, **kw)

    question = re.sub(rf"<@{bot_user_id()}>\s*", "", event.get("text", "")).strip()
    photo_ids = _save_photos(event.get("photos") or [], event.get("user", ""))
    if photo_ids and not question:
        question = "(sent a photo)"
    if not question:
        post("Hi! Ask me something.")
        return
    user = event.get("user", "")
    turn_id = None
    try:
        role, why = audience(event, user)
        log.info("slack answer in %s at role %s", event["channel"], role, extra={"json_fields": why})
        r = service.run_turn(question, thread_id=thread_ts or event["channel"], role=role, email=None,
                             uid=f"slack-{user}", channel="slack", client_key=f"slack:{user}",
                             attachments=photo_ids)
        reply = to_slack(r["answer"], r["citations"])
        turn_id = r.get("turn_id")
    except service.RateLimited:
        reply = "You've hit the question limit for now. Try again a bit later."
    except Exception:
        log.exception("agent call failed")
        reply = "Something went wrong on my side. Please try again in a minute."
    post(reply, blocks(reply, turn_id))


# ---------------- feedback ----------------

def blocks(text: str, turn_id: str | None) -> list[dict]:
    """The answer as section blocks (3,000-char limit each), then 👍 / 👎 buttons."""
    out, rest = [], text
    while rest:
        cut = len(rest) if len(rest) <= 2900 else (rest.rfind("\n", 0, 2900) if rest.rfind("\n", 0, 2900) > 0 else 2900)
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": rest[:cut]}})
        rest = rest[cut:].lstrip("\n")
    if turn_id:
        out.append({"type": "actions", "block_id": "feedback", "elements": [
            {"type": "button", "action_id": "fb_up", "value": turn_id, "text": {"type": "plain_text", "text": "👍"}},
            {"type": "button", "action_id": "fb_down", "value": turn_id, "text": {"type": "plain_text", "text": "👎"}},
        ]})
    return out


def _mark_rated(body: dict, label: str) -> None:
    msg = body.get("message") or {}
    kept = [b for b in msg.get("blocks", []) if b.get("block_id") != "feedback"]
    kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": label}]})
    bolt().client.chat_update(channel=body["channel"]["id"], ts=msg["ts"], text=msg.get("text", ""), blocks=kept)


def _on_thumb(ack, body, action):
    ack()
    up = action["action_id"] == "fb_up"
    user = body["user"]["id"]
    service.record_feedback(action["value"], 1 if up else -1, None, f"slack-{user}")
    if not up:
        bolt().client.views_open(trigger_id=body["trigger_id"], view={
            "type": "modal", "callback_id": "fb_comment", "private_metadata": action["value"],
            "title": {"type": "plain_text", "text": "What was wrong?"},
            "submit": {"type": "plain_text", "text": "Send"}, "close": {"type": "plain_text", "text": "Skip"},
            "blocks": [{"type": "input", "block_id": "c", "optional": True, "label": {"type": "plain_text", "text": "Tell us (optional)"},
                        "element": {"type": "plain_text_input", "action_id": "v", "multiline": True, "max_length": 1000,
                                    "placeholder": {"type": "plain_text", "text": "Wrong fact, missing info, bad source…"}}}],
        })
    try:
        _mark_rated(body, "👍 Thanks for the feedback" if up else "👎 Thanks, we'll use this to improve")
    except Exception:
        log.warning("couldn't update rated message", exc_info=True)


def _on_comment(ack, body, view):
    ack()
    comment = ((view["state"]["values"].get("c") or {}).get("v") or {}).get("value")
    if comment:
        service.record_feedback(view["private_metadata"], None, comment, f"slack-{body['user']['id']}")


def _wanted(event: dict) -> bool:
    return event.get("subtype") in (None, "file_share") and not event.get("bot_id") and bool(event.get("user"))


def _photos(event: dict) -> list[dict]:
    """Image files shared with the message (only Slack-hosted URLs, so the bot token stays on Slack)."""
    from urllib.parse import urlparse
    out = []
    for f in event.get("files") or []:
        url = f.get("url_private") or ""
        host = urlparse(url).hostname or ""
        if (f.get("mimetype") or "").startswith("image/") and url.startswith("https://") and host.endswith(".slack.com"):
            out.append({"url": url, "size": f.get("size") or 0})
    return out[:4]


def _save_photos(files: list[dict], user: str) -> list[str]:
    import httpx
    from . import uploads
    ids = []
    for f in files:
        if f["size"] > uploads.MAX_BYTES:
            continue
        try:
            r = httpx.get(f["url"], headers={"authorization": f"Bearer {settings.slack_bot_token}"},
                          timeout=20, follow_redirects=False)
            r.raise_for_status()
            ids.append(uploads.save(r.content, channel="slack", owner=f"slack:{user}"))
        except Exception:
            log.warning("couldn't save a Slack photo", exc_info=True)
    return ids


def _on_mention(event, body):
    if _wanted(event):
        dispatch(event, body.get("event_id"))


def _on_message(event, body):
    # Channel messages need an @mention (handled above); here, only direct messages.
    if event.get("channel_type") == "im" and _wanted(event):
        dispatch(event, body.get("event_id"))


# ---------------- ticket buttons (board channel) ----------------

def _on_ticket_button(ack, body, action, respond):
    ack()
    from . import notify, tickets
    user = body["user"]["id"]
    role = role_for(user)
    if role not in ("staff", "security", "board", "admin"):
        respond(text="Only LHCA staff and board members can update tickets.", response_type="ephemeral",
                replace_original=False)
        return
    status = "acknowledged" if action["action_id"] == "tk_ack" else "resolved"
    name = body["user"].get("name") or user
    try:
        t = tickets.update(int(action["value"]), role=role, actor=f"slack:{name}", status=status,
                           assignee=name if status == "acknowledged" else None)
    except Exception:
        log.exception("ticket button failed")
        respond(text="Couldn't update the ticket. Try the admin console.", response_type="ephemeral",
                replace_original=False)
        return
    notify.refresh(t, f"{'Acknowledged' if status == 'acknowledged' else 'Resolved'} by <@{user}>")


# ---------------- board votes ----------------

def _on_vote_button(ack, body, action, respond):
    ack()
    from . import votes
    user = body["user"]["id"]
    email = None
    try:
        email = bolt().client.users_info(user=user)["user"].get("profile", {}).get("email")
    except Exception:
        log.warning("users.info failed for a voter", exc_info=True)
    person = votes.voter(slack_user_id=user, slack_email=email)
    if not person:
        respond(text="Only seated board members can vote. Board terms are set in Users & roles.",
                response_type="ephemeral", replace_original=False)
        return
    vote = "yes" if action["action_id"] == "cv_yes" else "no"
    try:
        votes.cast(int(action["value"]), vote, person, "slack")
    except votes.VoteError as e:
        respond(text=e.detail, response_type="ephemeral", replace_original=False)
        return
    respond(text=f"Recorded: you voted to {'approve' if vote == 'yes' else 'reject'}. You can change it while the vote is open.",
            response_type="ephemeral", replace_original=False)


# ---------------- hand-off ----------------

KEEP = ("channel", "channel_type", "user", "text", "ts", "thread_ts", "placeholder_ts", "photos")


def _placeholder(event: dict) -> str | None:
    """Post "Cypress is thinking…" right away, where the answer will appear; the answer replaces it."""
    try:
        return bolt().client.chat_postMessage(channel=event["channel"], thread_ts=event.get("thread_ts"),
                                              text=THINKING)["ts"]
    except Exception:
        log.warning("couldn't post thinking placeholder", exc_info=True)
        return None


def dispatch(event: dict, event_id: str | None) -> None:
    payload = {k: event[k] for k in KEEP if k in event}
    if photos := _photos(event):
        payload["photos"] = photos
    if ts := _placeholder(event):
        payload["placeholder_ts"] = ts
    if not settings.slack_queue:
        threading.Thread(target=answer, args=(payload,), daemon=True).start()  # dev
        return
    from google.auth import default
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    task_id = re.sub(r"[^A-Za-z0-9_-]", "-", event_id or f"{payload['channel']}-{payload['ts']}")
    task = {"task": {
        "name": f"{settings.slack_queue}/tasks/{task_id}",
        "dispatchDeadline": "90s",
        "httpRequest": {
            "url": f"{settings.self_url}/api/slack/work", "httpMethod": "POST",
            "headers": {"content-type": "application/json"},
            "body": base64.b64encode(json.dumps(payload).encode()).decode(),
            "oidcToken": {"serviceAccountEmail": settings.run_sa, "audience": settings.self_url},
        },
    }}
    r = AuthorizedSession(creds).post(f"https://cloudtasks.googleapis.com/v2/{settings.slack_queue}/tasks",
                                      json=task, timeout=5)
    if r.status_code == 409:
        log.info("duplicate Slack event %s ignored", task_id)
    elif not r.ok:
        log.error("enqueue failed: %s %s", r.status_code, r.text[:300])


def _verify_task(request: Request) -> None:
    """Only Cloud Tasks, signing as our own service account, may call /work."""
    from .auth import require_internal
    require_internal(request)


# ---------------- routes ----------------

def enabled() -> bool:
    return bool(settings.slack_bot_token and settings.slack_signing_secret)


@router.post("/api/slack/events")
async def events(request: Request):
    if not enabled():
        raise HTTPException(404)
    if request.headers.get("x-slack-retry-num"):
        # Slack re-sends when our ack was slow (cold start). The first delivery is already being
        # handled, and a retry would post a second "thinking" placeholder.
        return Response(status_code=200, headers={"x-slack-no-retry": "1"})
    from slack_bolt.adapter.fastapi import SlackRequestHandler
    return await SlackRequestHandler(bolt()).handle(request)


@router.post("/api/slack/work")
async def work(request: Request):
    if not enabled():
        raise HTTPException(404)
    _verify_task(request)
    event = await request.json()
    await run_in_threadpool(answer, {k: event[k] for k in KEEP if k in event})
    return {"ok": True}  # always 200: the queue doesn't retry, and a failed reply was already reported
