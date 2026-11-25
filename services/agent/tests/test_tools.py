"""Role-gated tools: the model is only offered allowed tools, and call() re-checks anyway."""

from cypress.agent import graph, tools


def _board_only(monkeypatch):
    secret = tools.Tool("member_secret", "test", {"type": "object", "properties": {}}, ("board", "admin"),
                        lambda a, c: {"ok": True, "uid": c.uid})
    monkeypatch.setitem(tools.TOOLS, secret.name, secret)


def test_offered_tools_follow_role(monkeypatch):
    _board_only(monkeypatch)
    assert "member_secret" not in {t.name for t in tools.for_role(None)}
    assert "member_secret" not in {t.name for t in tools.for_role("security")}
    assert "member_secret" in {t.name for t in tools.for_role("board")}
    assert "check_deeded_access" in {t.name for t in tools.for_role(None)}


def test_call_rechecks_role_and_identity_comes_from_server(monkeypatch):
    _board_only(monkeypatch)
    denied = tools.call("member_secret", {"uid": "spoofed"}, tools.Ctx(role=None, uid=None, channel="web"))
    assert "error" in denied
    ok = tools.call("member_secret", {"uid": "spoofed"}, tools.Ctx(role="board", uid="u1", channel="web"))
    assert ok == {"ok": True, "uid": "u1"}
    assert "error" in tools.call("no_such_tool", {}, tools.Ctx(role="admin", uid="u1", channel="web"))


def test_routing_to_act_node():
    assert graph.after_route({"intent": "deeded_access", "role": None}) == "act"
    assert graph.after_route({"intent": "member_lookup", "role": None}) == "reply"
    assert graph.after_route({"intent": "question", "role": None}) == "retrieve"


def test_deeded_access_validates_input():
    assert "error" in tools.call("check_deeded_access", {"address": "x"}, tools.Ctx(None, None, "web"))


def test_ticket_tools_gating_and_identity(monkeypatch):
    from cypress import notify
    from cypress import tickets as tk
    calls = []
    monkeypatch.setattr(tk, "create", lambda **kw: calls.append(kw) or {"id": 1001, "status": "new", "priority": kw["priority"]})
    monkeypatch.setattr(notify, "new_ticket", lambda t, photos=0: True)
    public = {t.name for t in tools.for_role(None)}
    assert "report_issue" in public and "list_tickets" not in public and "update_ticket" not in public
    assert {"list_tickets", "update_ticket"} <= {t.name for t in tools.for_role("security")}

    out = tools.call("report_issue", {"title": "Gate broken", "description": "won't latch", "category": "pool",
                                      "urgency": "high", "reporter_role": "admin"},
                     tools.Ctx(role="member", uid="u9", channel="web", email="m@x.test"))
    assert out == {"filed": True, "ticket_number": 1001, "status": "new", "priority": "high", "photos_attached": 0, "board_notified": True}
    assert calls[0]["role"] == "member" and calls[0]["actor"] == "m@x.test"   # identity from the server, not args
    assert "error" in tools.call("list_tickets", {}, tools.Ctx(role="member", uid="u", channel="web"))


def test_digest_blocks():
    from datetime import UTC, datetime
    from cypress import digest
    now = datetime(2026, 11, 21, 14, tzinfo=UTC)
    rows = [
        {"id": 1, "title": "Gate", "status": "new", "priority": "high", "created_at": "2026-11-19T10:00:00Z",
         "updated_at": "2026-11-19T10:00:00Z", "assignee": None},
        {"id": 2, "title": "Light", "status": "acknowledged", "priority": "urgent", "created_at": "2026-11-21T09:00:00Z",
         "updated_at": "2026-11-21T09:00:00Z", "assignee": "Chris"},
        {"id": 3, "title": "Branch", "status": "resolved", "priority": "normal", "created_at": "2026-11-18T09:00:00Z",
         "updated_at": "2026-11-21T08:00:00Z", "assignee": None},
    ]
    b = digest.build(rows, now)
    text = "\n".join(x["text"]["text"] for x in b if x["type"] == "section")
    assert "2 open tickets" in text and "1 not yet acknowledged" in text and "1 resolved" in text
    assert text.index("#2 Light") < text.index("#1 Gate")          # urgent first
    assert digest.build([rows[2] | {"updated_at": "2026-11-10T00:00:00Z"}], now) is None
