"""Client for the Rust tickets service (services/tickets).

The service is private: on Cloud Run only this API's service account may invoke it, so requests
carry a Google ID token. The caller's verified role and identity go in x-cypress-role /
x-cypress-actor, and the service re-checks permissions with them.
"""

import logging
import time
from functools import lru_cache

import httpx

from .config import settings

log = logging.getLogger(__name__)


class TicketsError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


_token: tuple[float, str] = (0.0, "")


def _id_token() -> str | None:
    if not settings.tickets_url.startswith("https://"):
        return None  # local dev: plain http, no IAM
    global _token
    if time.time() - _token[0] < 45 * 60:
        return _token[1]
    import google.auth.transport.requests
    from google.oauth2 import id_token
    tok = id_token.fetch_id_token(google.auth.transport.requests.Request(), settings.tickets_url)
    _token = (time.time(), tok)
    return tok


@lru_cache
def _client() -> httpx.Client:
    return httpx.Client(base_url=settings.tickets_url, timeout=httpx.Timeout(15, connect=5))


def _req(method: str, path: str, *, role: str | None, actor: str, **kw) -> dict | list:
    headers = {"x-cypress-actor": actor[:200] or "anonymous"}
    if role:
        headers["x-cypress-role"] = role
    if tok := _id_token():
        headers["authorization"] = f"Bearer {tok}"
    r = _client().request(method, path, headers=headers, **kw)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise TicketsError(r.status_code, str(detail)[:300])
    return r.json()


def create(*, role, actor, **ticket) -> dict:
    return _req("POST", "/tickets", role=role, actor=actor, json=ticket)


def list_tickets(*, role, actor, status: str = "open", limit: int = 50) -> list:
    return _req("GET", "/tickets", role=role, actor=actor, params={"status": status, "limit": limit})


def get(ticket_id: int, *, role, actor) -> dict:
    return _req("GET", f"/tickets/{int(ticket_id)}", role=role, actor=actor)


def update(ticket_id: int, *, role, actor, **patch) -> dict:
    return _req("PATCH", f"/tickets/{int(ticket_id)}", role=role, actor=actor,
                json={k: v for k, v in patch.items() if v is not None})


def comment(ticket_id: int, body: str, *, role, actor) -> dict:
    return _req("POST", f"/tickets/{int(ticket_id)}/comments", role=role, actor=actor, json={"body": body})
