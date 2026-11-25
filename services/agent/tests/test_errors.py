"""Browser error reports and structured logs."""

import json
import logging

from cypress import logs
from cypress.db import tx


def test_client_error_is_logged_scrubbed_and_rate_limited(client, caplog):
    with tx() as conn:
        conn.execute("delete from rate_events where key like 'client_error:%%'")
    caplog.set_level(logging.ERROR, logger="cypress.client")
    r = client.post("/api/client-error", json={"kind": "sign-in", "code": "auth/internal-error",
                                               "message": "missing initial state for jane@example.com",
                                               "path": "/admin?x=1", "release": "abc123"})
    assert r.status_code == 204
    rec = caplog.records[-1]
    assert "missing initial state" in rec.getMessage() and "jane@example.com" not in rec.getMessage()
    assert rec.json_fields["client"]["path"] == "/admin" and rec.json_fields["serviceContext"]["service"] == "cypress-web"
    assert client.post("/api/client-error", json={"kind": "x", "message": "m" * 3000}).status_code == 422
    codes = {client.post("/api/client-error", json={"kind": "api", "message": "boom"}).status_code for _ in range(35)}
    assert 429 in codes


def test_cloud_json_format_includes_traceback():
    try:
        raise ValueError("bad thing")
    except ValueError:
        rec = logging.LogRecord("cypress.api", logging.ERROR, __file__, 1, "failed %s", ("here",), None)
        import sys
        rec.exc_info = sys.exc_info()
    out = json.loads(logs._CloudJson().format(rec))
    assert out["severity"] == "ERROR" and out["message"].startswith("failed here")
    assert "ValueError: bad thing" in out["message"]
