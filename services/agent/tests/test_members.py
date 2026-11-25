"""Membership list, member sign-in, reports that wait for sign-in, and Slack channel audiences."""

import pytest

from cypress import members, people, roles
from cypress.db import tx

CSV = b"""Household,Address,Account #,Email,Email 2,Status
Smith,104 Example Cove,A1,pat@smith.test,lee@smith.test,Active
Jones,12 Lakeview,A2,jo@jones.test,,Active
Brown,9 Hill Rd,A3,old@brown.test,,Lapsed
Bad,1 Nowhere,A4,not-an-email,,Active
Dupe,2 Nowhere,A5,pat@smith.test,,Active
"""


@pytest.fixture
def clean(client):
    with tx() as conn:
        conn.execute("delete from households")
        conn.execute("delete from pending_actions")
        conn.execute("delete from people where email like '%%.test'")
        conn.execute("delete from org_settings where key = 'member_auth'")
    from cypress import org
    org.clear()
    people.clear()
    yield
    people.clear()


def _import(client, data=CSV, replace=False):
    p = client.post("/api/admin/members/import/preview", files={"file": ("m.csv", data, "text/csv")}).json()
    import json
    r = client.post("/api/admin/members/import", files={"file": ("m.csv", data, "text/csv")},
                    data={"mapping": json.dumps(p["mapping"]), "replace": str(replace).lower()})
    return p, r.json()


def test_import_preview_and_commit(clean, client):
    p, r = _import(client)
    assert p["mapping"]["email1"] == "Email" and p["mapping"]["email2"] == "Email 2" and p["mapping"]["account_ref"] == "Account #"
    assert p["households"] == 5 and p["inactive"] == 1
    assert any("not-an-email" in x for x in p["problems"]) and any("also on line" in x for x in p["problems"])
    assert r["created"] == 5 and r["emails"] == 4
    listing = client.get("/api/admin/members?q=smith").json()
    assert listing["total"] == 1 and {e["email"] for e in listing["households"][0]["emails"]} == {"pat@smith.test", "lee@smith.test"}
    # Re-import with replace: matched by account ref, missing households go inactive.
    _, r2 = _import(client, b"Household,Account #,Email\nSmith,A1,pat@smith.test\n", replace=True)
    assert r2["updated"] == 1 and r2["deactivated"] == 3
    with tx() as conn:
        assert conn.execute("select count(*) as n from member_emails where email = 'lee@smith.test'").fetchone()["n"] == 0


def test_member_role_needs_verified_address_on_an_active_household(clean, client):
    _import(client)
    assert people.web_role("u1", "Pat@Smith.test", True) == "member"
    people.clear()
    assert people.web_role("u2", "jo@jones.test", False) is None          # unverified
    people.clear()
    assert people.web_role("u3", "old@brown.test", True) is None          # inactive household
    people.clear()
    assert people.web_role("u4", "stranger@x.test", True) is None
    # Board/staff listing wins over membership.
    client.post("/api/admin/people", json={"email": "jo@jones.test", "role": "security", "active": True, "groups": [], "terms": []})
    people.clear()
    assert people.web_role("u5", "jo@jones.test", True) == "security"


def test_anonymous_report_waits_for_member_sign_in(clean, client, monkeypatch):
    from cypress import notify, tickets
    from cypress.agent import tools
    _import(client)
    filed = []
    monkeypatch.setattr(tickets, "create", lambda **kw: filed.append(kw) or {"id": 77, "title": kw["title"], "status": "new"})
    monkeypatch.setattr(notify, "new_ticket", lambda t, photos=0: True)
    r = tools.call("report_issue", {"title": "Gate broken", "description": "Pool gate won't latch", "category": "pool",
                                    "urgency": "high", "location": "pool"}, tools.Ctx(role=None, uid=None, channel="web"))
    assert r["needs_member_sign_in"] and not filed
    pid = r["pending_id"]
    assert client.get(f"/api/chat/pending/{pid}").json()["title"] == "Gate broken"

    with pytest.raises(members.PendingError) as e:
        members.complete_pending(pid, role=None, email="stranger@x.test", uid="s")
    assert e.value.status == 403
    out = members.complete_pending(pid, role="member", email="pat@smith.test", uid="u1")
    assert out["ticket_number"] == 77 and filed[0]["actor"] == "pat@smith.test" and filed[0]["role"] == "member"
    assert members.complete_pending(pid, role="member", email="pat@smith.test", uid="u1")["ticket_number"] == 77
    assert len(filed) == 1                                                  # opening the link twice files once
    with pytest.raises(members.PendingError) as e:
        members.complete_pending(pid, role="member", email="jo@jones.test", uid="u2")
    assert e.value.status == 409


def test_expired_pending_and_setting_off(clean, client, monkeypatch):
    from cypress import org
    from cypress.agent import tools
    pid = members.create_pending("report_issue", {"title": "x", "description": "y"}, [], "web")
    with tx() as conn:
        conn.execute("update pending_actions set expires_at = now() - interval '1 minute' where id = %s", (pid,))
    with pytest.raises(members.PendingError) as e:
        members.complete_pending(pid, role="member", email="pat@smith.test", uid="u1")
    assert e.value.status == 410
    client.put("/api/admin/member-settings", json={"require_for_reports": False, "nudge_after_turns": 0, "pending_hours": 24})
    org.clear()
    from cypress import notify, tickets
    monkeypatch.setattr(tickets, "create", lambda **kw: {"id": 5, "status": "new", "priority": "normal"})
    monkeypatch.setattr(notify, "new_ticket", lambda t, photos=0: True)
    r = tools.call("report_issue", {"title": "a", "description": "b", "category": "other", "urgency": "low"},
                   tools.Ctx(role=None, uid=None, channel="web"))
    assert r["ticket_number"] == 5                                         # filed anonymously when the setting is off


def test_common_role():
    assert roles.common_role(["board", "board"]) == "board"
    assert roles.common_role(["board", "security"]) == "security"
    assert roles.common_role(["admin", "staff", "security"]) == "member"
    assert roles.common_role(["board", None]) is None
    assert roles.common_role([]) == "board"


def test_slack_channel_audience(client, monkeypatch):
    from cypress import slack
    monkeypatch.setattr(slack, "bot_user_id", lambda: "UBOT")
    monkeypatch.setattr(slack, "role_for", lambda u: {"UB1": "board", "UB2": "board"}.get(u))
    listed = {"UB1": "board", "UB2": "board", "UG": "security"}
    monkeypatch.setattr(slack, "member_role", lambda u: listed.get(u))
    monkeypatch.setattr(slack, "channel_ceiling", lambda c: False)

    monkeypatch.setattr(slack, "channel_members", lambda c: ["UB1", "UB2", "UBOT"])
    assert slack.audience({"channel": "G1", "channel_type": "group"}, "UB1")[0] == "board"
    monkeypatch.setattr(slack, "channel_members", lambda c: ["UB1", "UB2", "UG", "UBOT"])
    assert slack.audience({"channel": "G2", "channel_type": "group"}, "UB1")[0] == "security"   # guard present
    monkeypatch.setattr(slack, "channel_members", lambda c: ["UB1", "UNEW"])
    assert slack.audience({"channel": "G3", "channel_type": "group"}, "UB1")[0] is None         # unlisted = public
    monkeypatch.setattr(slack, "channel_members", lambda c: None)
    assert slack.audience({"channel": "C4", "channel_type": "channel"}, "UB1")[0] is None       # unknown: fail closed
    assert slack.audience({"channel": "D5", "channel_type": "im"}, "UB1")[0] == "board"         # DM: asker's role
    monkeypatch.setattr(slack, "channel_members", lambda c: ["UB1", "UB2"])
    monkeypatch.setattr(slack, "channel_ceiling", lambda c: "security")
    assert slack.audience({"channel": "G6", "channel_type": "group"}, "UB1")[0] == "security"   # ceiling lowers


def test_unlisted_slack_members_never_get_the_default_in_channels(clean, client):
    from cypress import org
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('slack_default_role', '"board"', 't')
                        on conflict (key) do update set value = excluded.value""")
    org.clear()
    assert people.slack_role("UZZ", "nobody@x.test") == "board"
    assert people.slack_role("UZZ", "nobody@x.test", use_default=False) is None
