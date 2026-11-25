"""Slack HTTP endpoints: signature checks, de-duplicated hand-off, and the guarded worker."""

import hashlib
import hmac
import json
import time

import pytest

from cypress import slack


def test_to_slack_formats_markdown_and_sources():
    out = slack.to_slack("### Hours\n**Open** daily\n- Tue–Sun [1]", [{"n": 1, "uri": "https://x/y", "title": "Pool"}])
    assert out.startswith("*Hours*\n*Open* daily\n• Tue–Sun [1]")
    assert out.endswith("[1] <https://x/y|Pool>")


def test_events_and_work_are_404_when_slack_not_configured(client):
    assert client.post("/api/slack/events", content=b"{}").status_code == 404
    assert client.post("/api/slack/work", json={}).status_code == 404


@pytest.fixture
def slack_on(monkeypatch):
    from dataclasses import replace
    s = replace(slack.settings, slack_bot_token="xoxb-test", slack_signing_secret="shh",
                self_url="https://svc.example", run_sa="run@example.iam.gserviceaccount.com")
    monkeypatch.setattr(slack, "settings", s)
    slack.bolt.cache_clear()
    yield s
    if hasattr(slack.bolt, "cache_clear"):
        slack.bolt.cache_clear()


def _signed(body: dict, secret="shh", ts=None):
    raw = json.dumps(body).encode()
    ts = str(ts or int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"x-slack-request-timestamp": ts, "x-slack-signature": sig, "content-type": "application/json"}


def test_bad_signature_rejected_and_good_one_dispatches(client, slack_on, monkeypatch):
    from slack_bolt import App
    monkeypatch.setattr(slack, "bolt", lambda: _app(App))
    seen = []
    monkeypatch.setattr(slack, "dispatch", lambda ev, eid: seen.append((ev["text"], eid)))
    body = {"type": "event_callback", "event_id": "Ev1", "team_id": "T1", "api_app_id": "A1",
            "event": {"type": "app_mention", "user": "U1", "text": "<@UBOT> pool hours?", "channel": "C1", "ts": "1.2"}}
    raw, headers = _signed(body, secret="wrong")
    assert client.post("/api/slack/events", content=raw, headers=headers).status_code == 401
    raw, headers = _signed(body)
    assert client.post("/api/slack/events", content=raw, headers=headers).status_code == 200
    assert seen == [("<@UBOT> pool hours?", "Ev1")]


def _app(App):
    from slack_bolt.authorization import AuthorizeResult
    app = App(signing_secret="shh", process_before_response=True,
              authorize=lambda **kw: AuthorizeResult(enterprise_id=None, team_id="T1", bot_token="xoxb-test",
                                                      bot_user_id="UBOT", bot_id="B1"))
    app.event("app_mention")(lambda event, body: slack._on_mention(event, body))
    app.event("message")(lambda event, body: slack._on_message(event, body))
    return app


def test_work_requires_cloud_tasks_identity(client, slack_on):
    assert client.post("/api/slack/work", json={"channel": "C1", "text": "hi"}).status_code == 401
    r = client.post("/api/slack/work", json={}, headers={"authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
