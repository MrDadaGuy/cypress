"""Thumbs up/down on chat turns, and the admin review list."""

from cypress.agent import service


def _turn():
    return service._log("web", "t" * 32, None, None, "pool hours?", {"intent": "question"}, "1-8 pm [1]", 900, None)


def test_feedback_round_trip(client):
    turn = _turn()
    assert turn
    assert client.post("/api/chat/feedback", json={"turn_id": turn, "rating": -1}).status_code == 200
    assert client.post("/api/chat/feedback", json={"turn_id": turn, "comment": "Hours are wrong"}).status_code == 200
    rows = client.get("/api/admin/interactions", params={"rating": "down"}).json()["items"]
    row = next(r for r in rows if r["public_id"] == turn)
    assert row["rating"] == -1 and row["feedback_comment"] == "Hours are wrong"
    detail = client.get(f"/api/admin/interactions/{row['id']}").json()
    assert detail["answer"] == "1-8 pm [1]" and detail["thread"][-1]["id"] == row["id"]


def test_feedback_validation(client):
    turn = _turn()
    assert client.post("/api/chat/feedback", json={"turn_id": turn}).status_code == 422
    assert client.post("/api/chat/feedback", json={"turn_id": turn, "rating": 5}).status_code == 422
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.post("/api/chat/feedback", json={"turn_id": missing, "rating": 1}).status_code == 404


def test_board_member_questions_get_volunteer_reply(client):
    from cypress import org
    from cypress.agent import graph
    r = client.put("/api/admin/org", json={"board_members": ["Pat Example", "Lee Sample"],
                                           "board_contact": {"name": "CAM, our management company", "email": "admin@camanagers.com"}})
    assert r.status_code == 200 and r.json()["board_members"] == ["Pat Example", "Lee Sample"]
    assert org.mentioned_board_member("what's pat example's phone number?") == "Pat Example"
    assert org.mentioned_board_member("I talked to Sample yesterday") == "Lee Sample"
    assert org.mentioned_board_member("send me a sample form") is None   # lowercase word, not a name
    out = graph.reply({"intent": "board_contact", "messages": [{"role": "user", "content": "how do I reach Pat Example?"}]})
    assert "volunteers" in out["answer"] and "admin@camanagers.com" in out["answer"] and "Pat Example" in out["answer"]
    roster = graph.reply({"intent": "board_contact", "messages": [{"role": "user", "content": "who is on the board?"}]})
    assert "Lee Sample" in roster["answer"]
    assert client.put("/api/admin/org", json={"board_members": [], "board_contact": {"name": "CAM", "email": "nope"}}).status_code == 422
