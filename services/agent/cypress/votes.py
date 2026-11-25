"""Board votes on incident notice letters.

When a case file has a draft letter, Cypress opens a vote in the board channel (Approve / Reject buttons) and
on the ticket page. The letter is marked ready for CAM only if the vote passes; Cypress still doesn't send it.

The rule is an admin setting (org_settings 'board_votes'):
  rule      "majority" of the seated board (seats // 2 + 1, so 4 of 7), or "count" = a fixed number of yes votes
  seats     board size used for the majority; the number of active board terms is used instead once any exist
  days      how long the vote stays open; then it expires as not passed
  auto_start  open the vote as soon as the case file is ready (else a board member starts it)
A vote passes when yes votes reach the threshold, and fails as soon as it can no longer pass. Members can change
their vote while it's open. Only people with an active board term vote; Slack users are matched to them by
their Slack id or email. Admin is not the same as board: an admin without a board term can't vote.
"""

import logging

from .audit import audit
from .db import tx

log = logging.getLogger(__name__)
DEFAULTS = {"rule": "majority", "count": 4, "seats": 7, "days": 3, "auto_start": True}


class VoteError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def settings() -> dict:
    from . import org
    return {**DEFAULTS, **(org.get().get("board_votes") or {})}


def seated() -> int:
    with tx() as conn:
        return conn.execute("select count(*)::int as n from people_access where on_board and active").fetchone()["n"]


def threshold(s: dict | None = None) -> tuple[int, int]:
    """(yes votes needed, seats)."""
    s = s or settings()
    seats = seated() or int(s["seats"])
    if s["rule"] == "count":
        return max(1, min(int(s["count"]), seats)), seats
    return seats // 2 + 1, seats


def voter(*, email: str | None = None, slack_user_id: str | None = None, slack_email: str | None = None) -> dict | None:
    """The board member behind a console login or a Slack user, or None if they don't hold a board seat."""
    with tx() as conn:
        return conn.execute(
            """select id, coalesce(name, email) as name from people_access
               where on_board and active and (lower(email) = lower(%(e)s) or slack_user_id = %(s)s
                                              or lower(email) = lower(%(se)s)) limit 1""",
            {"e": email or "", "s": slack_user_id or "", "se": slack_email or ""}).fetchone()


def tally(case_id: int) -> dict:
    with tx() as conn:
        rows = conn.execute("""select v.vote, v.at, v.via, coalesce(p.name, p.email) as name from case_votes v
                               join people p on p.id = v.person_id where v.case_id = %s order by v.at""", (case_id,)).fetchall()
    return {"yes": sum(r["vote"] == "yes" for r in rows), "no": sum(r["vote"] == "no" for r in rows),
            "votes": [{"name": r["name"], "vote": r["vote"], "at": r["at"], "via": r["via"]} for r in rows]}


def _case(ticket_id: int | None = None, case_id: int | None = None) -> dict | None:
    with tx() as conn:
        return conn.execute("select * from incident_cases where ticket_id = %s or id = %s",
                            (ticket_id or -1, case_id or -1)).fetchone()


def open_vote(case: dict, by: str) -> dict:
    if case["status"] != "awaiting_board":
        raise VoteError(409, "The case file isn't waiting for the board")
    if not case["letter"]:
        raise VoteError(409, "There's no letter to vote on")
    if case["vote_status"] == "open":
        raise VoteError(409, "A vote is already open")
    s = settings()
    needed, seats = threshold(s)
    with tx() as conn:
        conn.execute("delete from case_votes where case_id = %s", (case["id"],))
        case = conn.execute(
            """update incident_cases set vote_status = 'open', vote_opened_at = now(),
                 vote_closes_at = now() + make_interval(days => %s), vote_needed = %s, vote_seats = %s
               where id = %s returning *""", (int(s["days"]), needed, seats, case["id"])).fetchone()
    _post(case)
    log.info("board vote opened on case %s (needs %s of %s) by %s", case["id"], needed, seats, by)
    return case


def cast(ticket_id: int, vote: str, person: dict, via: str) -> dict:
    """Record (or change) a board member's vote; closes the vote when the outcome is decided."""
    c = _case(ticket_id=ticket_id)
    if not c or c["vote_status"] != "open":
        raise VoteError(409, "There's no open vote on this ticket")
    if expire_if_due(c):
        raise VoteError(409, "The vote has closed")
    with tx() as conn:
        conn.execute("""insert into case_votes (case_id, person_id, vote, via) values (%s, %s, %s, %s)
                        on conflict (case_id, person_id) do update set vote = excluded.vote, via = excluded.via, at = now()""",
                     (c["id"], person["id"], vote, via))
    t = tally(c["id"])
    if t["yes"] >= c["vote_needed"]:
        _close(c, "passed", t)
    elif t["no"] > c["vote_seats"] - c["vote_needed"]:
        _close(c, "failed", t)
    else:
        _update(c, t)
    return _case(case_id=c["id"])


def cancel(ticket_id: int, user) -> dict:
    c = _case(ticket_id=ticket_id)
    if not c or c["vote_status"] != "open":
        raise VoteError(409, "There's no open vote on this ticket")
    with tx() as conn:
        conn.execute("update incident_cases set vote_status = 'cancelled' where id = %s", (c["id"],))
        audit(conn, user, "case_vote_cancel", "ticket", ticket_id)
    _update(_case(case_id=c["id"]), tally(c["id"]), footer=f"Vote cancelled by {user.email}.")
    return _case(case_id=c["id"])


def expire_if_due(c: dict) -> bool:
    with tx() as conn:
        due = conn.execute("select %s < now() as due", (c["vote_closes_at"],)).fetchone()["due"] if c["vote_closes_at"] else False
    if c["vote_status"] == "open" and due:
        _close(c, "expired", tally(c["id"]))
        return True
    return False


def expire_due() -> int:
    """Nightly: close votes past their deadline."""
    with tx() as conn:
        rows = conn.execute("select * from incident_cases where vote_status = 'open' and vote_closes_at < now()").fetchall()
    for c in rows:
        _close(c, "expired", tally(c["id"]))
    return len(rows)


def _close(c: dict, outcome: str, t: dict) -> None:
    """Record the outcome and resume the case graph: passed = approved (ready for CAM); otherwise dismissed."""
    from . import cases
    with tx() as conn:
        conn.execute("update incident_cases set vote_status = %s where id = %s", (outcome, c["id"]))
    result = f"{t['yes']} for, {t['no']} against ({c['vote_needed']} of {c['vote_seats']} needed)"
    note = {"passed": f"Board vote passed, {result}. The letter is ready for CAM.",
            "failed": f"Board vote failed, {result}.",
            "expired": f"Board vote expired without passing, {result}."}[outcome]
    try:
        cases.resolve(c, "approve" if outcome == "passed" else "dismiss", note, by="board vote")
    except Exception:
        log.exception("closing the vote on case %s couldn't resume the case", c["id"])
    _update(_case(case_id=c["id"]), t, footer=note)
    log.info("board vote on case %s %s: %s", c["id"], outcome, result)


# ---------------- Slack ----------------

def _blocks(c: dict, t: dict, footer: str | None = None) -> list[dict]:
    from .config import settings as cfg
    letter = c["letter"] or {}
    summary = ((c["profile"] or {}).get("summary") or "")[:1200]
    open_ = c["vote_status"] == "open"
    status = (f"*{t['yes']} approve · {t['no']} reject* — {c['vote_needed']} of {c['vote_seats']} needed"
              + (f" · closes <!date^{int(c['vote_closes_at'].timestamp())}^{{date_short_pretty}} {{time}}|soon>" if open_ else ""))
    voters = ", ".join(f"{v['name']} ({'✅' if v['vote'] == 'yes' else '❌'})" for v in t["votes"]) or "No votes yet"
    link = f"{cfg.web_url.rstrip('/')}/admin/tickets/{c['ticket_id']}" if getattr(cfg, "web_url", None) else None
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🗳️ Board vote: notice letter for ticket #{c['ticket_id']}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary or "_No summary_"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{letter.get('subject', '')}*\n>{(letter.get('body') or '')[:2400].replace(chr(10), chr(10) + '>')}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": status}, {"type": "mrkdwn", "text": voters}]},
    ]
    if open_:
        buttons = [{"type": "button", "action_id": "cv_yes", "value": str(c["ticket_id"]), "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"}},
                   {"type": "button", "action_id": "cv_no", "value": str(c["ticket_id"]), "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"}}]
        if link:
            buttons.append({"type": "button", "action_id": "cv_view", "url": link, "text": {"type": "plain_text", "text": "Open the case file"}})
        blocks.append({"type": "actions", "elements": buttons})
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


def _post(c: dict) -> None:
    from .config import settings as cfg
    if not (cfg.slack_bot_token and cfg.slack_signing_secret):
        return
    from . import org
    from .slack import bolt
    channel = org.get().get("board_slack_channel") or "#community-association-board"
    try:
        r = bolt().client.chat_postMessage(channel=channel, text=f"Board vote: notice letter for ticket #{c['ticket_id']}",
                                           blocks=_blocks(c, tally(c["id"])), unfurl_links=False)
        with tx() as conn:
            conn.execute("update incident_cases set vote_slack_channel = %s, vote_slack_ts = %s where id = %s",
                         (r["channel"], r["ts"], c["id"]))
    except Exception:
        log.exception("couldn't post the board vote for case %s", c["id"])


def _update(c: dict, t: dict, footer: str | None = None) -> None:
    if not c.get("vote_slack_ts"):
        return
    from .slack import bolt
    try:
        bolt().client.chat_update(channel=c["vote_slack_channel"], ts=c["vote_slack_ts"],
                                  text=f"Board vote: notice letter for ticket #{c['ticket_id']}", blocks=_blocks(c, t, footer))
    except Exception:
        log.exception("couldn't update the board vote message for case %s", c["id"])


def view(c: dict, user_email: str | None) -> dict | None:
    if c["vote_status"] == "none":
        return None
    t = tally(c["id"])
    me = voter(email=user_email) if user_email else None
    mine = next((v["vote"] for v in t["votes"] if me and v["name"] == me["name"]), None)
    return {"status": c["vote_status"], "needed": c["vote_needed"], "seats": c["vote_seats"],
            "opened_at": c["vote_opened_at"], "closes_at": c["vote_closes_at"], **t,
            "can_vote": bool(me) and c["vote_status"] == "open", "my_vote": mine}


def check_after_open(case_id: int) -> None:
    """Used right after a case is ready: open the vote automatically if the setting says so."""
    if not settings()["auto_start"]:
        return
    c = _case(case_id=case_id)
    if c and c["letter"] and c["status"] == "awaiting_board":
        try:
            open_vote(c, "auto")
        except VoteError:
            pass


def board_ready() -> dict:
    """For the settings panel: is anyone able to vote?"""
    with tx() as conn:
        r = conn.execute("""select count(*) filter (where on_board and active)::int as seated,
                                   count(*) filter (where on_board and active and slack_user_id is not null)::int as on_slack
                            from people_access""").fetchone()
    needed, seats = threshold()
    return {**r, "needed": needed, "seats": seats}

