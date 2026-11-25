"""Suggest an owner for a new ticket from people's "areas of responsibility".

The model only picks from the listed people (by id) and its answer is checked against that list,
so a ticket's text can't route it to anyone else. It suggests; staff decide. If anything fails
the ticket simply goes out without a suggestion.
"""

import json
import logging

from . import models, people
from .agent import prompts

log = logging.getLogger(__name__)

def suggest(ticket: dict) -> dict:
    """{"people": [{"id", "name", "slack_user_id"}], "reason": str}; empty when there's no clear fit."""
    cands = people.routing_candidates()
    if not cands:
        return {"people": [], "reason": ""}
    roster = "\n".join(f"- id {c['id']}: {c['name']}{' (' + c['board_title'] + ')' if c['board_title'] else ''}"
                       f" ({c['effective_role']}): {c['responsibilities'].strip()[:600]}" for c in cands)
    ticket_txt = (f"Title: {ticket.get('title', '')}\nCategory: {ticket.get('category', '')}\n"
                  f"Location: {ticket.get('location') or 'not given'}\nDescription: {ticket.get('description', '')[:1500]}")
    try:
        text, _ = models.generate("route_ticket", prompts.system("route_ticket"), [{"role": "user", "content": f"People:\n{roster}\n\nTicket:\n{ticket_txt}"}],
                                  json_mode=True, max_tokens=200, temperature=0)
        out = json.loads(text)
        by_id = {c["id"]: c for c in cands}
        picked = [by_id[int(i)] for i in out.get("people", [])[:2] if str(i).isdigit() and int(i) in by_id]
        return {"people": [{"id": c["id"], "name": c["name"], "slack_user_id": c["slack_user_id"]} for c in picked],
                "reason": str(out.get("reason", ""))[:160] if picked else ""}
    except Exception:
        log.warning("ticket routing failed", exc_info=True)
        return {"people": [], "reason": ""}
