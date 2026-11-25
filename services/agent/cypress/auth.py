"""Firebase Auth: verify the ID token, then look up the person's role in the people table.

Roles and what each may see are defined in roles.py. Console access ranks admin > board > staff;
security, members and the anonymous public never reach /api/admin.
"""

from dataclasses import dataclass
from functools import lru_cache

from fastapi import Depends, HTTPException, Request

from .config import settings

# Console access: staff and security see Operations (tickets); admin pages need admin.
ROLE_RANK = {"staff": 1, "security": 1, "board": 2, "admin": 3}


@dataclass(frozen=True)
class User:
    uid: str
    email: str
    role: str | None

    def has(self, role: str) -> bool:
        return ROLE_RANK.get(self.role or "", 0) >= ROLE_RANK[role]


@lru_cache
def _firebase():
    import firebase_admin
    from firebase_admin import auth

    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"projectId": settings.project})
    return auth


def current_user(request: Request) -> User:
    if settings.env == "dev" and settings.dev_user:
        return User(uid="dev", email=settings.dev_user, role="admin")
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in required")
    try:
        claims = _firebase().verify_id_token(header[7:])
    except Exception:
        raise HTTPException(401, "Invalid or expired sign-in") from None
    from . import people
    email = claims.get("email", "")
    return User(uid=claims["uid"], email=email,
                role=people.web_role(claims["uid"], email, bool(claims.get("email_verified"))))


def require(role: str):
    def dep(user: User = Depends(current_user)) -> User:
        if not user.has(role):
            raise HTTPException(403, f"Requires the {role} role")
        return user

    return dep


def require_internal(request: Request) -> None:
    """Only Google-signed calls from our own service account (Cloud Tasks, Cloud Scheduler) pass."""
    from google.auth.transport.requests import Request as GRequest
    from google.oauth2 import id_token

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer ") or not settings.self_url or not settings.run_sa:
        raise HTTPException(401)
    try:
        claims = id_token.verify_oauth2_token(header[7:], GRequest(), audience=settings.self_url)
    except Exception:
        raise HTTPException(401) from None
    if claims.get("email") != settings.run_sa or not claims.get("email_verified"):
        raise HTTPException(403)
