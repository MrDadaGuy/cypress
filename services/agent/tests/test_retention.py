"""Retention cleanup and legal holds."""

import uuid

import pytest

from cypress import org, retention, uploads
from cypress.db import tx


def _settings(client, **kw):
    body = {"unattached_hours": 24, "closed_ticket_days": 180, "lookup_log_days": 365, "hold_all": False,
            "hold_all_reason": None, **kw}
    r = client.put("/api/admin/retention", json=body)
    assert r.status_code == 200, r.text
    org.clear()
    return r.json()


def _ticket(status: str, days_ago: int) -> int:
    with tx() as conn:
        return conn.execute(
            """insert into tickets (status, title, description, channel, updated_at)
               values (%s, 'Gate at 3106 Edgewater Dr', 'test', 'web', now() - make_interval(days => %s)) returning id""",
            (status, days_ago)).fetchone()["id"]


def _upload(ticket_id: int | None = None, hours_ago: int = 0, kind: str = "photo") -> str:
    uid = str(uuid.uuid4())
    path = f"{kind}s/{uid}.{'jpg' if kind == 'photo' else 'mp4'}"
    (uploads.LOCAL_DIR / path).parent.mkdir(parents=True, exist_ok=True)
    (uploads.LOCAL_DIR / path).write_bytes(b"x" * 100)
    with tx() as conn:
        conn.execute("""insert into uploads (id, channel, owner, path, bytes, kind, ticket_id, created_at)
                        values (%s, 'web', 'test', %s, 100, %s, %s, now() - make_interval(hours => %s))""",
                     (uid, path, kind, ticket_id, hours_ago))
    return uid


def _exists(uid: str) -> bool:
    with tx() as conn:
        return bool(conn.execute("select 1 from uploads where id = %s", (uid,)).fetchone())


def _removed(uid: str) -> bool:
    with tx() as conn:
        return conn.execute("select removed_at from uploads where id = %s", (uid,)).fetchone()["removed_at"] is not None


@pytest.fixture
def fresh(client):
    with tx() as conn:
        conn.execute("delete from legal_holds")
        conn.execute("delete from uploads")
        conn.execute("delete from retention_runs")
    _settings(client)
    yield
    _settings(client)


def test_unattached_uploads_go_after_a_day(fresh):
    old, new = _upload(hours_ago=30), _upload(hours_ago=2)
    out = retention.run()
    assert out["unattached"] == 1 and not _exists(old) and _exists(new)


def test_closed_ticket_attachments_go_open_tickets_never(fresh):
    closed_old, closed_new, open_old = _ticket("closed", 200), _ticket("resolved", 30), _ticket("in_progress", 900)
    a, v = _upload(closed_old, 5000), _upload(closed_old, 5000, kind="video")
    b, c = _upload(closed_new, 5000), _upload(open_old, 9000)
    out = retention.run()
    assert out["photos"] == 1 and out["videos"] == 1
    assert _removed(a) and _removed(v) and not _removed(b) and not _removed(c)
    assert uploads.read(a) is None and uploads.read(b) is not None
    gone = uploads.media_for_ticket(closed_old, removed=True)
    assert {m["kind"] for m in gone} == {"photo", "video"} and uploads.media_for_ticket(closed_old) == []
    assert retention.run()["photos"] == 0                                    # already removed: nothing again


def test_keep_forever(fresh, client):
    _settings(client, closed_ticket_days=0, lookup_log_days=0)
    a = _upload(_ticket("closed", 2000), 99999)
    assert retention.run()["photos"] == 0 and not _removed(a)


def test_legal_hold_on_a_ticket(fresh, client):
    t = _ticket("closed", 400)
    a = _upload(t, 9000)
    assert client.put(f"/api/admin/tickets/{t}/hold", json={"on": True, "reason": ""}).status_code == 422
    r = client.put(f"/api/admin/tickets/{t}/hold", json={"on": True, "reason": "Insurance claim, per board"})
    assert r.status_code == 200 and r.json()["hold"]["reason"] == "Insurance claim, per board"
    with tx() as conn:
        assert conn.execute("select held from uploads where id = %s", (a,)).fetchone()["held"]
        assert conn.execute("select count(*) as n from audit_log where action = 'legal_hold' and entity_id = %s",
                            (str(t),)).fetchone()["n"] == 1
    assert retention.run()["photos"] == 0 and not _removed(a)
    with pytest.raises(Exception):                                           # a held ticket can't be deleted
        with tx() as conn:
            conn.execute("delete from tickets where id = %s", (t,))
    client.put(f"/api/admin/tickets/{t}/hold", json={"on": False})
    with tx() as conn:
        assert not conn.execute("select held from uploads where id = %s", (a,)).fetchone()["held"]
    assert retention.run()["photos"] == 1 and _removed(a)
    assert client.put("/api/admin/tickets/99999999/hold", json={"on": True, "reason": "x y z"}).status_code == 404


def test_hold_everything(fresh, client):
    assert client.put("/api/admin/retention", json={"unattached_hours": 24, "closed_ticket_days": 180, "lookup_log_days": 365,
                                                    "hold_all": True, "hold_all_reason": ""}).status_code == 422
    view = _settings(client, hold_all=True, hold_all_reason="Lawsuit, per counsel")
    assert view["settings"]["hold_all"] and view["stored"]["held"] == 0
    old = _upload(hours_ago=99)
    a = _upload(_ticket("closed", 999), 99999)
    with tx() as conn:
        conn.execute("insert into member_lookups (at, actor, channel, mode, result) values (now() - interval '3 years', 't', 'web', 'confirm', 'not_found')")
    out = retention.run()
    assert out["hold_all"] and out["unattached"] == out["photos"] == out["lookups"] == 0
    assert _exists(old) and not _removed(a)
    with tx() as conn:
        assert conn.execute("select held from uploads where id = %s", (a,)).fetchone()["held"]
    _settings(client)                                                         # released
    out = retention.run()
    assert out["unattached"] == 1 and out["photos"] == 1 and out["lookups"] >= 1


def test_bucket_holds_follow_the_setting(fresh, client, monkeypatch):
    calls = []
    monkeypatch.setattr(retention, "_set_object_hold", lambda path, on: calls.append((path, on)) or True)
    t = _ticket("in_progress", 1)
    _upload(t)
    retention.set_hold(t, True, "Claim", type("U", (), {"email": "b@x.test", "role": "board"})())
    assert [on for _, on in calls] == [True]
    assert retention.sync_holds() == 0                                        # already in sync
    monkeypatch.setattr(retention, "_delete_object", lambda path: False)     # e.g. the bucket refused
    with tx() as conn:
        conn.execute("delete from legal_holds")
    assert retention.run()["photos"] == 0 and calls[-1][1] is False          # hold cleared on the next run


def test_digest_line_and_nightly(fresh, monkeypatch):
    from cypress import digest, recrawl
    assert digest.retention_blocks(None) == []
    assert digest.retention_blocks({"photos": 0, "videos": 0, "unattached": 0, "lookups": 0, "failed": 0}) == []
    text = digest.retention_blocks({"photos": 2, "videos": 1, "unattached": 3, "lookups": 1, "failed": 0})[0]["text"]["text"]
    assert "2 photos and 1 video" in text and "3 uploads" in text and "1 old member lookup log entry" in text and "MB" not in text
    monkeypatch.setattr(recrawl, "run", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out = recrawl.nightly()
    assert out["recrawl"] == {"error": "RuntimeError"} and "photos" in out["retention"]
    assert retention.overnight()["photos"] == 0
