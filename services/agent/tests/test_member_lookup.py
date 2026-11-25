"""Member lookup: who sees what, confirm mode never fills gaps, rate limits, logging, and routing."""

import pytest

from cypress import member_lookup as ml
from cypress import org
from cypress.agent import tools
from cypress.db import tx

CSV = b"""Household,Address,Email,Status
Smith,"104 Example Cove, Austin TX 78746",pat@smith.test,Active
Brown,9 Hill Rd,old@brown.test,Lapsed
"""


@pytest.fixture
def homes(client):
    with tx() as conn:
        conn.execute("delete from households")
        conn.execute("delete from member_lookups")
        conn.execute("delete from rate_events where key like 'lookup:%%'")
        conn.execute("delete from org_settings where key = 'member_lookup'")
        a = conn.execute("insert into households (name, address, status) values ('Smith', '104 Example Cove, Austin TX 78746', 'active') returning id").fetchone()["id"]
        conn.execute("insert into member_emails (household_id, email, name) values (%s, 'pat@smith.test', 'Pat Smith')", (a,))
        conn.execute("insert into households (name, address, status) values ('Brown', '9 Hill Rd', 'inactive')")
    org.clear()
    yield
    with tx() as conn:
        conn.execute("delete from org_settings where key = 'member_lookup'")
    org.clear()


def look(role="security", **kw):
    return ml.lookup(address=kw.get("address"), name=kw.get("name"), role=role, actor=kw.get("actor", "guard@x.test"), channel="web")


def test_address_normalization():
    assert ml.norm_address("104 Example Cove, Austin, TX 78746") == "104 example cv"
    assert ml._same_address(ml.norm_address("104 example cv."), ml.norm_address("104 Example Cove"))
    assert ml._same_address("104 example", "104 example cv")
    assert not ml._same_address("10 example cv", "104 example cv")


def test_confirm_mode_is_yes_no_and_reveals_nothing_new(homes):
    r = look(address="104 Example Cv")
    assert r["active_member_household"] is True and "matches" not in r and "Smith" not in str(r)
    assert look(address="9 Hill Road")["active_member_household"] is False        # inactive looks like no match
    assert "inactive" not in str(look(address="9 Hill Road"))
    assert look(name="Pat Smith")["active_member_household"] is True and "104" not in str(look(name="Pat Smith"))
    both = look(address="104 Example Cove", name="Lee Jones")
    assert both["active_member_household"] is False and "isn't on file" in both["note"]
    assert look(address="104 Example Cove", name="Pat Smith")["active_member_household"] is True


def test_details_mode_never_includes_emails(homes):
    r = look("board", address="104 example cove")
    assert r["matches"][0]["household"] == "Smith" and r["matches"][0]["people_on_file"] == ["Pat Smith"]
    assert "@" not in str(r)
    assert look("admin", name="brown")["matches"][0]["status"] == "inactive"


def test_members_public_and_switched_off_roles_get_nothing(homes, client):
    assert "error" in look("member", address="104 Example Cove")
    assert "error" in look(None, address="104 Example Cove")
    assert not tools.TOOLS["lookup_member"].allows("member") and not tools.TOOLS["lookup_member"].allows(None)
    s = client.get("/api/admin/member-lookup").json()["settings"]
    s["access"]["security"] = "off"
    assert client.put("/api/admin/member-lookup", json=s).status_code == 200
    assert "error" in look("security", address="104 Example Cove")
    assert not tools.TOOLS["lookup_member"].allows("security")
    r = tools.call("lookup_member", {"address": "104 Example Cove"}, tools.Ctx(role="security", uid="g", channel="slack"))
    assert "isn't available" in r["error"]


def test_every_lookup_is_logged_and_rate_limited(homes, client):
    s = client.get("/api/admin/member-lookup").json()["settings"]
    client.put("/api/admin/member-lookup", json={**s, "per_hour": 2})
    look(address="104 Example Cove")
    look(name="Pat Smith")
    assert "limit" in look(address="9 Hill Rd")["error"]
    log = client.get("/api/admin/member-lookup").json()["log"]
    assert [r["result"] for r in log] == ["rate_limited", "active_member", "active_member"]
    assert log[0]["actor"] == "guard@x.test" and log[0]["role"] == "security"


def test_evals_use_the_fictional_directory(homes):
    token = tools.DRY_RUN.set(True)
    try:
        r = tools.call("lookup_member", {"address": "123 Example Lane"}, tools.Ctx(role="board", uid="e", channel="eval"))
    finally:
        tools.DRY_RUN.reset(token)
    assert r["matches"][0]["household"] == "Rivera"
    with tx() as conn:
        assert conn.execute("select count(*) as n from member_lookups").fetchone()["n"] == 0


def test_routing_sends_members_to_the_fixed_reply():
    from cypress.agent import graph
    assert graph.after_route({"intent": "member_lookup", "role": "member"}) == "reply"
    assert graph.after_route({"intent": "member_lookup", "role": None}) == "reply"


def test_new_eval_batch_is_added_once(client):
    from cypress import evals
    with tx() as conn:
        conn.execute("delete from org_settings where key = 'eval_seed_batches'")
        conn.execute("delete from eval_cases where 'member-lookup' = any(tags)")
    evals.seed_if_empty("t")                       # table may be fresh or not; batch gets added
    with tx() as conn:
        n = conn.execute("select count(*) as n from eval_cases where 'member-lookup' = any(tags)").fetchone()["n"]
        conn.execute("delete from eval_cases where id = (select min(id) from eval_cases where 'member-lookup' = any(tags))")
    assert n == len(evals.BATCHES["member-lookup"])
    evals.seed_if_empty("t")                       # a deleted case doesn't come back
    with tx() as conn:
        assert conn.execute("select count(*) as n from eval_cases where 'member-lookup' = any(tags)").fetchone()["n"] == n - 1
