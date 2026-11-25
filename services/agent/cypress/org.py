"""Organization facts (board roster, how to reach the board), cached for a few minutes.

Board members are volunteers. When someone asks about one, Cypress doesn't speculate about the
person or hand out personal contact details; it points to the management company instead.
"""

import logging
import re
import time

from .db import tx

log = logging.getLogger(__name__)
TTL = 300
DEFAULTS = {"board_members": [], "board_contact": {"name": "CAM, our management company", "email": "admin@camanagers.com"},
            # Suggested questions on the resident chat's welcome screen (editable in Board & contacts).
            "chat_suggestions": ["Is the park open to the public?", "What are the park hours?",
                                 "Can I bring my dog to the park?", "How do I rent the clubhouse?",
                                 "How can I tell if an address has deeded access?"]}
_cache: tuple[float, dict] = (0.0, {})


def get() -> dict:
    global _cache
    if time.time() - _cache[0] < TTL:
        return _cache[1]
    data = dict(DEFAULTS)
    try:
        with tx() as conn:
            for r in conn.execute("select key, value from org_settings"):
                data[r["key"]] = r["value"]
    except Exception:
        log.exception("reading org settings failed; using defaults")
    # Board members with an active term (Users & roles) replace the hand-kept list once any exist.
    data["board_members_source"] = "manual"
    try:
        from . import people
        roster = people.board_roster()
        if roster:
            data["board_members"] = [r["name"] for r in roster]
            data["board_members_source"] = "terms"
    except Exception:
        log.exception("reading the board roster failed; using the saved list")
    _cache = (time.time(), data)
    return data


def clear() -> None:
    global _cache
    _cache = (0.0, {})


def mentioned_board_member(text: str) -> str | None:
    """A board member named in the message: full name, or a capitalized surname as a whole word."""
    for name in get()["board_members"]:
        parts = name.split()
        if re.search(rf"\b{re.escape(name)}\b", text, re.I):
            return name
        if len(parts) > 1 and len(parts[-1]) > 3 and re.search(rf"\b{re.escape(parts[-1])}\b", text):
            return name
    return None


def board_reply(member: str | None) -> str:
    o = get()
    c = o["board_contact"]
    names = ", ".join(o["board_members"])
    lead = f"{member} serves on the LHCA board. " if member else ""
    roster = f"The current board members are {names}. " if names and not member else ""
    return (f"{lead}{roster}Our board members are all volunteers, so the best way to reach the board is "
            f"through {c['name']}: **{c['email']}**. They'll make sure your message gets to the right person.")
