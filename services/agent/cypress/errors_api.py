"""Browser error reports and the catch-all for unhandled server errors.

The web app sends uncaught errors, failed sign-ins and failed chat streams here, so problems
people hit in their browser show up in Cloud Logging / Error Reporting next to the server's own.
Reports are anonymous: no user id, email or message text, only what went wrong and where.
"""

import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .agent import service
from .chat_api import client_ip

log = logging.getLogger("cypress.client")
router = APIRouter(tags=["errors"])

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _scrub(s: str | None, n: int) -> str:
    return _EMAIL.sub("<email>", (s or "")[:n])


class ClientError(BaseModel):
    kind: str = Field(max_length=40)          # window-error | unhandled-rejection | sign-in | chat-stream | api
    message: str = Field(max_length=2000)
    code: str | None = Field(default=None, max_length=100)
    stack: str | None = Field(default=None, max_length=8000)
    path: str | None = Field(default=None, max_length=300)
    release: str | None = Field(default=None, max_length=60)


@router.post("/api/client-error", status_code=204)
async def client_error(body: ClientError, request: Request):
    try:
        await run_in_threadpool(service.check_rate, f"client_error:{client_ip(request)}", "client_error")
    except service.RateLimited:
        raise HTTPException(429, "Too many error reports") from None
    msg = _scrub(body.message, 2000)
    stack = _scrub(body.stack, 8000)
    log.error("browser %s: %s%s", body.kind, msg, f"\n{stack}" if stack else "",
              extra={"json_fields": {
                  "client": {"kind": body.kind, "code": body.code, "path": (body.path or "").split("?")[0],
                             "release": body.release, "user_agent": request.headers.get("user-agent", "")[:300]},
                  # Error Reporting groups by service; give browser errors their own.
                  "serviceContext": {"service": "cypress-web", "version": body.release or "unknown"},
                  # Lets Error Reporting pick up reports that have no stack trace (e.g. sign-in errors).
                  "@type": "type.googleapis.com/google.devtools.clouderrorreporting.v1beta1.ReportedErrorEvent"}})


async def unhandled(request: Request, exc: Exception):
    logging.getLogger("cypress.api").error("unhandled error on %s %s", request.method, request.url.path,
                                           exc_info=exc)
    return JSONResponse({"detail": "Something went wrong on our side. It has been logged."}, status_code=500)
