"""Scheduled re-crawls: which sources are due, retries after failures, and the digest line."""

from datetime import UTC, datetime

import pytest

from cypress import digest, recrawl
from cypress.db import tx


@pytest.fixture
def sources(client):
    with tx() as conn:
        conn.execute("delete from jobs")
        conn.execute("delete from sources")
        conn.execute("delete from document_sets where slug like 'rc-%%'")
        set_id = conn.execute("""insert into document_sets (slug, name, created_by) values ('rc-site', 'Site', 't') returning id""").fetchone()["id"]
        ids = {}
        for name, sched in (("weekly", "weekly"), ("daily", "daily"), ("manual", "manual")):
            ids[name] = conn.execute("""insert into sources (set_id, kind, url, schedule, created_by)
                                        values (%s, 'web', %s, %s, 't') returning id""",
                                     (set_id, f"https://example.org/{name}", sched)).fetchone()["id"]
    return set_id, ids


def _job(set_id, sid, status, ago):
    with tx() as conn:
        conn.execute("insert into jobs (set_id, source_id, status, triggered_by, created_at) values (%s, %s, %s, 't', now() - %s::interval)",
                     (set_id, sid, status, ago))


def test_never_crawled_scheduled_sources_are_due(sources):
    _, ids = sources
    assert {r["id"] for r in recrawl.due()} == {ids["weekly"], ids["daily"]}


def test_weekly_waits_a_week_daily_waits_a_day(sources):
    set_id, ids = sources
    _job(set_id, ids["weekly"], "succeeded", "3 days")
    _job(set_id, ids["daily"], "succeeded", "21 hours")
    assert {r["id"] for r in recrawl.due()} == {ids["daily"]}
    _job(set_id, ids["weekly"], "succeeded", "7 days")      # older success doesn't matter; latest does
    assert ids["weekly"] not in {r["id"] for r in recrawl.due()}


def test_failed_crawl_retries_next_night_and_running_is_skipped(sources):
    set_id, ids = sources
    _job(set_id, ids["weekly"], "succeeded", "3 days")
    _job(set_id, ids["weekly"], "failed", "21 hours")
    assert ids["weekly"] in {r["id"] for r in recrawl.due()}
    _job(set_id, ids["daily"], "running", "1 minute")
    assert ids["daily"] not in {r["id"] for r in recrawl.due()}


def test_run_queues_jobs_once(sources, monkeypatch):
    _, ids = sources
    out = recrawl.run()
    assert len(out["queued"]) == 2
    assert recrawl.run()["queued"] == []                     # already queued
    with tx() as conn:
        assert conn.execute("select count(*) as n from jobs where triggered_by = 'schedule'").fetchone()["n"] == 2


def test_disabled_source_or_set_is_never_due(sources):
    set_id, ids = sources
    with tx() as conn:
        conn.execute("update sources set enabled = false where id = %s", (ids["daily"],))
        conn.execute("update document_sets set enabled = false where id = %s", (set_id,))
    assert recrawl.due() == []


def test_next_run_is_the_nightly_slot():
    assert recrawl.next_run(datetime(2026, 11, 20, 12, 0, tzinfo=UTC)).isoformat().startswith("2026-11-21T03:30")
    assert recrawl.next_run(datetime(2026, 11, 20, 9, 0, tzinfo=UTC)).isoformat().startswith("2026-11-20T03:30")


def test_admin_set_shows_next_check(sources, client):
    set_id, ids = sources
    srcs = {s["id"]: s for s in client.get(f"/api/admin/sets/{set_id}").json()["sources"]}
    assert srcs[ids["manual"]]["next_check"] is None and srcs[ids["weekly"]]["next_check"]


def test_internal_endpoint_needs_google_identity(client):
    assert client.post("/api/internal/recrawl").status_code == 401


def test_digest_line_only_when_something_changed():
    assert digest.recrawl_blocks(None) == []
    assert digest.recrawl_blocks({"jobs": 1, "new": 0, "updated": 0, "gone": 0, "failed": 0}) == []
    t = digest.recrawl_blocks({"jobs": 1, "new": 1, "updated": 2, "gone": 0, "failed": 1})[0]["text"]["text"]
    assert "1 new, 2 updated" in t and "retried tonight" in t
    assert digest.build([], recrawl={"jobs": 1, "new": 0, "updated": 1, "gone": 0, "failed": 0})
