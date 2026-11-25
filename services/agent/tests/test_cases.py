"""Incident case files: the supervisor's plan guardrails, the matcher's no-invented-matches rule, who sees
the full case file, and the board's decision resuming the paused graph."""

import json

import pytest

from cypress import cases, models
from cypress.auth import User


def fake_models(monkeypatch, *, plan=None, match=None, letter=True):
    """Stub every model call in the case graph; tool calls go to the real (fictional, dry-run) lookups."""
    def generate(node, system, messages, **kw):
        if node == "case_plan":
            return json.dumps(plan or {"agents": ["matcher", "history", "policy"], "reason": "test"}), "m"
        if node == "case_policy":
            return json.dumps({"rules": [{"rule": "No overnight parking at the boat ramp", "source": 1, "why": "it was overnight"}],
                               "process": "Written notice, then a hearing before any fine.", "confidence": "high"}), "m"
        if node == "case_writer":
            return json.dumps({"summary": "A car was parked overnight.", "assessment": "Exact plate match.",
                               "next_step": "Send the notice.", "letter": {"subject": "Notice", "body": "Dear neighbor, " * 3} if letter else None}), "m"
        raise AssertionError(node)

    def generate_with_tools(node, system, messages, tools, run_tool, **kw):
        calls = []
        for plate in ["ZZT1234"]:
            calls.append({"tool": "lookup_plate", "args": {"plate": plate}, "result": run_tool("lookup_plate", {"plate": plate})})
        return json.dumps(match or {"matches": [{"household": "Rivera", "address": "123 Example Lane", "basis": "exact plate ZZT1234",
                                                 "confidence": "high"}], "summary": "Exact plate match."}), "m", calls
    monkeypatch.setattr(models, "generate", generate)
    monkeypatch.setattr(models, "generate_with_tools", generate_with_tools)
    monkeypatch.setattr("cypress.retrieval.search", lambda q, role, k=6: ([_P()], {}))


class _P:
    title, section, uri, content = "Park Rules", "Parking", "https://example.test/rules", "No overnight parking at the boat ramp."


REPORT = {"title": "Car parked overnight at the boat ramp", "what": "Blue SUV left overnight in the ramp lane",
          "where": "boat ramp", "plates": ["ZZT-1234"]}


def test_dry_run_builds_a_full_case_file(client, monkeypatch):
    fake_models(monkeypatch)
    v = cases.run_dry(REPORT)
    assert v["plan"]["agents"] == ["matcher", "history", "policy"]
    assert set(v["steps"]) >= {"supervisor", "matcher", "history", "policy", "writer"} and "board" not in v["steps"]  # paused
    h = v["profile"]["households"][0]
    assert h["name"] == "Rivera" and h["vehicles"][0]["car"] == "blue 2019 Honda CR-V"
    assert v["letter"]["subject"] == "Notice"
    assert v["findings"]["policy"]["sources"][0]["title"] == "Park Rules"


def test_plan_guardrails(client, monkeypatch):
    # The model asks for the plate reader without photos and skips policy: code fixes both.
    fake_models(monkeypatch, plan={"agents": ["plate_reader", "matcher"]})
    v = cases.run_dry(REPORT)
    assert v["plan"]["agents"] == ["matcher", "history", "policy"]
    # Nothing identifying: no matcher or history at all.
    fake_models(monkeypatch, plan={"agents": ["matcher", "history", "policy"]})
    v = cases.run_dry({"title": "Noise", "what": "Loud music at the pavilion after hours"})
    assert v["plan"]["agents"] == ["policy"] and v["profile"]["households"] == [] and v["letter"] is None


def test_matcher_drops_invented_matches_and_weak_letters(client, monkeypatch):
    fake_models(monkeypatch, match={"matches": [{"household": "Made Up", "address": "1 Fake St", "confidence": "high"},
                                                {"household": "Rivera", "address": "123 Example Lane", "confidence": "low"}]})
    v = cases.run_dry(REPORT)
    assert [h["name"] for h in v["households"]] == ["Rivera"]            # "Made Up" never came from a tool
    assert v["profile"]["households"] == [] and v["letter"] is None       # low confidence: no contact block, no letter


@pytest.fixture
def board(client, monkeypatch):
    """Seven seated board members (fictional), and a case file waiting for the board with a draft letter."""
    from cypress import member_lookup, org, people, tickets
    from cypress.db import tx
    fake_models(monkeypatch)
    monkeypatch.setattr(tickets, "comment", lambda *a, **k: None)
    monkeypatch.setattr("cypress.ingest.trigger.start_ingest", lambda: None)
    real = member_lookup.lookup_plate
    monkeypatch.setattr(member_lookup, "lookup_plate", lambda **kw: real(**{**kw, "dry_run": True}))
    monkeypatch.setattr(cases, "_resolve_ids", lambda hh, dry: [{**h, "id": None} for h in hh])
    with tx() as conn:
        conn.execute("delete from incident_cases")
        conn.execute("delete from people where email like '%%@board.test'")
        conn.execute("delete from org_settings where key = 'board_votes'")
        ids = []
        for i in range(1, 8):
            pid = conn.execute("""insert into people (email, name, active, created_by) values (%s, %s, true, 'test')
                                  returning id""", (f"d{i}@board.test", f"Director {i}")).fetchone()["id"]
            conn.execute("""insert into board_terms (person_id, start_date, end_date) values (%s, current_date - 30, current_date + 300)""", (pid,))
            ids.append(pid)
        t = conn.execute("""insert into tickets (title, description, category, priority, channel, reporter_role)
                            values ('Car at the ramp overnight', 'Blue SUV, plate ZZT-1234', 'security', 'normal', 'web', 'security')
                            returning id""").fetchone()
    org.clear()
    people.clear()
    cases.open_case(t["id"], REPORT, [], "guard@x.test")
    cases.run(cases.claim())          # auto_start opens the vote
    yield {"ticket": t["id"], "people": [{"id": i, "name": f"Director {n}"} for n, i in enumerate(ids, 1)]}
    with tx() as conn:
        conn.execute("delete from incident_cases")
        conn.execute("delete from people where email like '%%@board.test'")


def test_vote_rule_is_configurable(board, client):
    from cypress import org, votes
    from cypress.db import tx
    assert votes.threshold() == (4, 7)                      # majority of the seated board
    with tx() as conn:
        conn.execute("""insert into org_settings (key, value, updated_by) values ('board_votes', '{"rule": "count", "count": 5}', 't')
                        on conflict (key) do update set value = excluded.value""")
    org.clear()
    assert votes.threshold() == (5, 7)
    with tx() as conn:
        conn.execute("""update org_settings set value = '{"rule": "count", "count": 9}' where key = 'board_votes'""")
    org.clear()
    assert votes.threshold() == (7, 7)                      # never more than the seats


def test_majority_vote_passes_the_letter(board, client):
    from cypress import votes
    from cypress.auth import User
    tid, ppl = board["ticket"], board["people"]
    c = cases.view(tid, User("u", "d1@board.test", "board"))
    assert c["vote"]["status"] == "open" and c["vote"]["needed"] == 4 and c["vote"]["can_vote"]
    # A single board member can't approve a letter outside the vote.
    assert client.put(f"/api/admin/tickets/{tid}/case", json={"action": "approve"}).status_code == 409
    for p in ppl[:3]:
        votes.cast(tid, "yes", p, "slack")
    votes.cast(tid, "no", ppl[3], "console")
    votes.cast(tid, "yes", ppl[3], "console")               # changed their mind: 4 yes
    c = cases.view(tid, User("u", "d1@board.test", "board"))
    assert c["status"] == "approved" and c["vote"]["status"] == "passed" and c["vote"]["yes"] == 4
    assert "4 for, 0 against" in c["decision"]["note"] and c["decided_by"] == "board vote"
    import pytest as _p
    with _p.raises(votes.VoteError):
        votes.cast(tid, "no", ppl[5], "slack")              # closed


def test_vote_fails_once_it_cannot_pass_and_only_board_members_vote(board, client):
    from cypress import votes
    tid, ppl = board["ticket"], board["people"]
    # The console's dev user is an admin with no board term: admin isn't board.
    r = client.post(f"/api/admin/tickets/{tid}/case/vote", json={"vote": "yes"})
    assert r.status_code == 403
    assert votes.voter(email="nobody@x.test") is None
    for p in ppl[:4]:
        votes.cast(tid, "no", p, "slack")                   # 4 against: at most 3 yes left, can't reach 4
    from cypress.db import tx
    with tx() as conn:
        c = conn.execute("select status, vote_status from incident_cases where ticket_id = %s", (tid,)).fetchone()
    assert c["status"] == "dismissed" and c["vote_status"] == "failed"


def test_votes_expire_and_letters_lock_while_open(board, client):
    from cypress import votes
    from cypress.db import tx
    tid = board["ticket"]
    r = client.put(f"/api/admin/tickets/{tid}/case/letter", json={"subject": "New", "body": "A different letter for the household."})
    assert r.status_code == 409                             # can't change the words mid-vote
    assert client.delete(f"/api/admin/tickets/{tid}/case/vote").status_code == 200     # admin cancels
    r = client.put(f"/api/admin/tickets/{tid}/case/letter", json={"subject": "New", "body": "A different letter for the household."})
    assert r.status_code == 200 and r.json()["case"]["letter"]["subject"] == "New"
    assert client.post(f"/api/admin/tickets/{tid}/case/vote/open").status_code == 200
    with tx() as conn:
        conn.execute("update incident_cases set vote_closes_at = now() - interval '1 minute' where ticket_id = %s", (tid,))
    assert votes.expire_due() == 1
    with tx() as conn:
        c = conn.execute("select status, vote_status from incident_cases where ticket_id = %s", (tid,)).fetchone()
    assert c["status"] == "dismissed" and c["vote_status"] == "expired"


def test_full_case_file_only_for_configured_roles(board, client):
    from cypress.auth import User
    slim = cases.view(board["ticket"], User("u2", "g@x.test", "security"))
    assert set(slim) == {"status", "matched", "full", "created_at", "finished_at", "decided_at"} and not slim["full"]
