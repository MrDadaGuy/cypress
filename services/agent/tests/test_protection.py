"""Abuse protection: App Check modes, admin-editable limits, and the anonymous daily spend cap."""

import dataclasses

import pytest

from cypress import org, protection
from cypress.agent import service
from cypress.config import settings
from cypress.db import tx


def _set(client, **kw):
    s = {**protection.DEFAULTS, **kw}
    assert client.put("/api/admin/protection", json=s).status_code == 200
    org.clear()
    protection.clear()


@pytest.fixture
def fresh(client):
    with tx() as conn:
        conn.execute("delete from protection_events")
        conn.execute("delete from app_check_daily")
        conn.execute("delete from rate_events")
    yield
    _set(client)


class Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_limits_come_from_admin_settings_and_log_only_a_hash(fresh, client):
    _set(client, anon_per_hour=2)
    for _ in range(2):
        service.check_rate("chat:ip:203.0.113.9", "anon")
    with pytest.raises(service.RateLimited):
        service.check_rate("chat:ip:203.0.113.9", "anon")
    with tx() as conn:
        rows = conn.execute("select reason, endpoint, visitor from protection_events").fetchall()
    assert [(r["reason"], r["endpoint"]) for r in rows] == [("rate_hour", "chat")]
    assert "203.0.113.9" not in rows[0]["visitor"] and len(rows[0]["visitor"]) == 10


def test_visitor_over_the_limit_is_asked_to_sign_in(fresh, client):
    _set(client, anon_per_hour=1, app_check="off")
    with tx() as conn:
        conn.execute("insert into rate_events (key) values ('chat:ip:testclient')")
    r = client.post("/api/chat/stream", json={"message": "hi"})
    assert r.status_code == 429 and r.json()["sign_in"] is True and "Sign in" in r.json()["detail"]


def test_app_check_monitor_lets_through_and_enforce_blocks(fresh, client, monkeypatch):
    monkeypatch.setattr(protection, "settings", dataclasses.replace(settings, env="prod"))
    _set(client, app_check="monitor")
    protection.gate(Req(), None, "chat", "chat:ip:x")                       # no token: logged, let through
    _set(client, app_check="enforce")
    with pytest.raises(protection.Blocked) as e:
        protection.gate(Req({"x-firebase-appcheck": "forged"}), None, "chat", "chat:ip:x")
    assert e.value.status == 403 and e.value.reason == "invalid"
    protection.gate(Req(), object(), "chat", "uid")                          # signed in: never checked
    monkeypatch.setattr(protection, "app_check", lambda req: "verified")
    protection.gate(Req({"x-firebase-appcheck": "good"}), None, "chat", "chat:ip:x")
    s = client.get("/api/admin/protection").json()
    assert s["app_check"] == {"missing": 1, "invalid": 1, "verified": 1}
    assert {(r["reason"], r["enforced"]) for r in s["by_reason"]} == {("app_check_missing", False), ("app_check_invalid", True)}


def test_dev_without_a_token_skips_app_check(fresh, client):
    _set(client, app_check="enforce")
    protection.gate(Req(), None, "chat", "chat:ip:x")
    with tx() as conn:
        assert conn.execute("select count(*) as n from app_check_daily").fetchone()["n"] == 0


def test_anonymous_spend_cap_pauses_visitors_not_members(fresh, client, monkeypatch):
    _set(client, anon_daily_usd=2, app_check="off")
    monkeypatch.setattr(protection, "anon_spend_today", lambda: 2.5)
    r = client.post("/api/chat/stream", json={"message": "hi"})
    assert r.status_code == 429 and r.json()["reason"] == "spend_cap"
    protection.gate(Req(), object(), "chat", "uid", spend=True)              # members keep chatting
    _set(client, anon_daily_usd=0, app_check="off")                           # 0 = no cap
    protection.gate(Req(), None, "chat", "chat:ip:x", spend=True)
    assert client.get("/api/admin/protection").json()["by_reason"][0]["reason"] == "spend_cap"


def test_anon_spend_counts_only_anonymous_web_calls(fresh, client):
    with tx() as conn:
        conn.execute("delete from llm_calls")
        conn.execute("""insert into llm_calls (node, model_id, provider, cost_usd, channel, user_role) values
                        ('answer', 'm', 'gemini', 0.5, 'web', null), ('answer', 'm', 'gemini', 1, 'web', 'member'),
                        ('answer', 'm', 'gemini', 1, 'slack', null), ('answer', 'm', 'gemini', 1, 'eval', null)""")
    protection.clear()
    assert protection.anon_spend_today() == 0.5


def test_settings_validation(client):
    bad = {**protection.DEFAULTS, "app_check": "maybe"}
    assert client.put("/api/admin/protection", json=bad).status_code == 422


def test_off_topic_strikes_ask_visitors_to_sign_in(fresh, client, monkeypatch):
    _set(client, app_check="off", off_topic_strikes=3)

    def fake_turn(message, **kw):
        yield {"type": "done", "answer": "I can only help with LHCA topics.", "intent": "off_topic"}
    monkeypatch.setattr(service, "stream_turn", fake_turn)
    for _ in range(3):
        assert client.post("/api/chat/stream", json={"message": "write my essay"}).status_code == 200
    r = client.post("/api/chat/stream", json={"message": "write my essay"})
    assert r.status_code == 429 and r.json()["reason"] == "off_topic_strikes" and r.json()["sign_in"] is True
    protection.gate(Req(), object(), "chat", "uid", spend=True)              # members are never struck
    _set(client, app_check="off", off_topic_strikes=0)                         # 0 = off
    assert client.post("/api/chat/stream", json={"message": "write my essay"}).status_code == 200
    reasons = {(r["reason"], r["enforced"]) for r in client.get("/api/admin/protection").json()["by_reason"]}
    assert ("off_topic", False) in reasons and ("off_topic_strikes", True) in reasons
