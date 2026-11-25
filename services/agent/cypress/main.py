"""Cypress API. Run locally:

    CYPRESS_DEV_USER=you@example.com .venv/bin/uvicorn cypress.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import chat_api, digest, errors_api, logs, protection, recrawl, slack, tracing
from .admin import (content, content_checks, conversations, evals_admin, members_admin, models_admin, org_admin, people_admin,
                    protection_admin, retention_admin, tickets_admin)
from .agent import service as chat_service
from .auth import User, current_user
from .config import settings
from .db import migrate, pool

logs.setup()
tracing.setup()


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()
    migrate()
    chat_service.graph()  # build the graph and checkpointer tables now, not on the first chat
    yield
    chat_service.close()
    pool.close()


app = FastAPI(title="Cypress", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["*"],
    allow_headers=["authorization", "content-type", "x-firebase-appcheck"],
)
app.include_router(content.router)
app.include_router(conversations.router)
app.include_router(content_checks.router)
app.include_router(evals_admin.router)
app.include_router(members_admin.router)
app.include_router(models_admin.router)
app.include_router(org_admin.router)
app.include_router(people_admin.router)
app.include_router(protection_admin.router)
app.include_router(retention_admin.router)
app.include_router(tickets_admin.router)
app.include_router(chat_api.router)
app.include_router(slack.router)
app.include_router(digest.router)
app.include_router(recrawl.router)
app.include_router(errors_api.router)
app.add_exception_handler(Exception, errors_api.unhandled)
tracing.instrument_app(app)


@app.exception_handler(protection.Blocked)
async def blocked(_, e: protection.Blocked):
    # sign_in tells the chat to show the member sign-in card with the message.
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": e.message, "sign_in": True, "reason": e.reason}, status_code=e.status)


@app.get("/health")
def healthz():
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"email": user.email, "role": user.role}
