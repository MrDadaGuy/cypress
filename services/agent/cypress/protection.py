"""Abuse protection for the public chat.

Anonymous residents can chat without signing in, and every question is a paid model call, so:

- App Check: the web app gets a short-lived token from Firebase App Check (reCAPTCHA Enterprise,
  invisible) and sends it with each request. Scripts calling the API directly don't have one. Modes:
  off, monitor (count and log, let through) and enforce (anonymous requests without a valid token are
  asked to sign in). Signed-in callers skip the check, so an ad blocker never locks out a member.
- Rate limits per visitor (IP for anonymous, account for signed-in), editable by admins.
- Off-topic strikes: the first step already sorts each question with the cheapest model, and an off-topic
  one gets a fixed reply with no further model calls. A visitor who keeps asking off-topic things (default 3
  in an hour) is asked to sign in, so probing stops before the rate limit. No extra model cost.
- A daily cap on anonymous model spend: past it, anonymous chat pauses with a "sign in" message while
  members keep chatting. This protects the monthly budget from one bad actor.

Visitors are logged only as a short hash of their rate-limit key, never as an IP address.
"""

import datetime as dt
import hashlib
import logging
import random
import time

from fastapi import Request

from .config import settings
from .db import tx

log = logging.getLogger(__name__)

DEFAULTS = {"app_check": "monitor", "anon_per_hour": 20, "anon_per_day": 60, "user_per_hour": 120,
            "user_per_day": 400, "anon_daily_usd": 2, "off_topic_strikes": 3}
STRIKE_WINDOW_MIN = 60
MODES = ("off", "monitor", "enforce")
HEADER = "x-firebase-appcheck"
KEEP_DAYS = 90
_anon: dict = {"at": 0.0, "day": None, "usd": 0.0}
SPEND_TTL = 30


class Blocked(Exception):
    """A request refused for protection reasons; the chat answers with a sign-in prompt."""

    def __init__(self, status: int, message: str, reason: str):
        super().__init__(message)
        self.status, self.message, self.reason = status, message, reason


def get() -> dict:
    from . import org
    return {**DEFAULTS, **(org.get().get("protection") or {})}


def limits(kind: str) -> list[tuple[int, int]]:
    """(window seconds, max requests) for 'anon' or 'user' callers."""
    s = get()
    return [(3600, int(s[f"{kind}_per_hour"])), (86400, int(s[f"{kind}_per_day"]))]


def visitor(key: str) -> str:
    return hashlib.sha256(f"{settings.project}:{key}".encode()).hexdigest()[:10]


def record(reason: str, endpoint: str, key: str, enforced: bool = True) -> None:
    try:
        with tx() as conn:
            conn.execute("insert into protection_events (reason, endpoint, visitor, enforced) values (%s, %s, %s, %s)",
                         (reason, endpoint, visitor(key), enforced))
            if random.random() < 0.02:   # tidy up now and then
                conn.execute("delete from protection_events where at < now() - make_interval(days => %s)", (KEEP_DAYS,))
        log.warning("protection: %s on %s", reason, endpoint,
                    extra={"json_fields": {"protection": {"reason": reason, "endpoint": endpoint,
                                                          "visitor": visitor(key), "enforced": enforced}}})
    except Exception:
        log.exception("recording a protection event failed")


def _count(outcome: str) -> None:
    try:
        with tx() as conn:
            conn.execute("""insert into app_check_daily (day, outcome, n) values ((now() at time zone 'America/Chicago')::date, %s, 1)
                            on conflict (day, outcome) do update set n = app_check_daily.n + 1""", (outcome,))
    except Exception:
        log.exception("counting App Check outcome failed")


def app_check(request: Request) -> str:
    """'verified', 'missing' or 'invalid'."""
    token = request.headers.get(HEADER)
    if not token:
        return "missing"
    try:
        from firebase_admin import app_check as fac
        from .auth import _firebase
        _firebase()                      # initializes the Firebase app
        fac.verify_token(token)
        return "verified"
    except Exception as e:
        log.info("App Check token rejected: %s", type(e).__name__)
        return "invalid"


def anon_spend_today() -> float:
    """Model spend on anonymous web chat today (America/Chicago), cached briefly."""
    now, day = time.time(), dt.date.today().isoformat()
    if now - _anon["at"] < SPEND_TTL and _anon["day"] == day:
        return _anon["usd"]
    try:
        with tx() as conn:
            usd = conn.execute(
                """select coalesce(sum(cost_usd), 0)::float as usd from llm_calls
                   where channel = 'web' and user_role is null
                     and at >= date_trunc('day', now() at time zone 'America/Chicago') at time zone 'America/Chicago'""").fetchone()["usd"]
        _anon.update(at=now, day=day, usd=usd)
    except Exception:
        log.exception("reading anonymous spend failed")
    return _anon["usd"]


def clear() -> None:
    _anon.update(at=0.0, day=None, usd=0.0)


SIGN_IN = "Sign in with your member email to keep going."


def gate(request: Request, user, endpoint: str, key: str, *, spend: bool = False) -> None:
    """Checks for an anonymous web request before any paid work. Raises Blocked."""
    if user is not None:
        return
    s = get()
    mode = s.get("app_check") if s.get("app_check") in MODES else "monitor"
    # Local development has no App Check site; only check a token if one is sent.
    if mode != "off" and not (settings.env == "dev" and not request.headers.get(HEADER)):
        outcome = app_check(request)
        _count(outcome)
        if outcome != "verified":
            record(f"app_check_{outcome}", endpoint, key, enforced=mode == "enforce")
            if mode == "enforce":
                raise Blocked(403, f"I couldn't confirm this request came from the Cypress website. {SIGN_IN}", outcome)
    limit = int(s.get("off_topic_strikes") or 0)
    if spend and limit and strikes(key) >= limit:
        record("off_topic_strikes", endpoint, key)
        raise Blocked(429, "I can only help with Lake Hills Community Association topics, and the last few questions "
                           f"weren't about the association. {SIGN_IN}", "off_topic_strikes")
    cap = float(s.get("anon_daily_usd") or 0)
    if spend and cap and anon_spend_today() >= cap:
        record("spend_cap", endpoint, key)
        raise Blocked(429, f"Cypress has answered a lot of questions from visitors today. {SIGN_IN}", "spend_cap")


def strike(key: str, endpoint: str = "chat") -> None:
    """An anonymous visitor asked something off-topic (logged as flagged, not blocked)."""
    if int(get().get("off_topic_strikes") or 0) > 0:
        record("off_topic", endpoint, key, enforced=False)


def strikes(key: str) -> int:
    try:
        with tx() as conn:
            return conn.execute(
                """select count(*)::int as n from protection_events where reason = 'off_topic' and visitor = %s
                     and at > now() - make_interval(mins => %s)""", (visitor(key), STRIKE_WINDOW_MIN)).fetchone()["n"]
    except Exception:
        log.exception("counting off-topic strikes failed")
        return 0


def summary(days: int = 7) -> dict:
    with tx() as conn:
        by_reason = conn.execute(
            """select reason, enforced, count(*)::int as n, count(distinct visitor)::int as visitors, max(at) as last
               from protection_events where at > now() - make_interval(days => %s)
               group by reason, enforced order by n desc""", (days,)).fetchall()
        daily = conn.execute(
            """select (at at time zone 'America/Chicago')::date::text as day, count(*) filter (where enforced)::int as blocked,
                      count(*) filter (where not enforced)::int as flagged
               from protection_events where at > now() - make_interval(days => %s) group by 1 order by 1""", (days,)).fetchall()
        top = conn.execute(
            """select visitor, count(*)::int as n, max(at) as last, array_agg(distinct reason) as reasons
               from protection_events where at > now() - make_interval(days => %s)
               group by visitor order by n desc limit 5""", (days,)).fetchall()
        checks = {r["outcome"]: r["n"] for r in conn.execute(
            """select outcome, sum(n)::int as n from app_check_daily
               where day > (now() at time zone 'America/Chicago')::date - %s group by outcome""", (days,))}
    return {"days": days, "by_reason": by_reason, "daily": daily, "top_visitors": top, "app_check": checks,
            "anon_spend_today": round(anon_spend_today(), 4)}
