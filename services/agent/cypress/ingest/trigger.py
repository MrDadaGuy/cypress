"""Start the Cloud Run ingest job when work is queued.

Locally (no CYPRESS_INGEST_JOB) this does nothing and the long-running worker picks jobs up.
The job drains the whole queue with SKIP LOCKED, so an extra execution is harmless.
"""

import logging
import threading

from ..config import settings

log = logging.getLogger(__name__)


def _run(job: str) -> None:
    try:
        import google.auth
        import google.auth.transport.requests
        from google.auth.transport.requests import AuthorizedSession

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        r = AuthorizedSession(creds).post(f"https://run.googleapis.com/v2/{job}:run", json={}, timeout=15)
        if r.status_code >= 300:
            log.error("starting ingest job failed: %s %s", r.status_code, r.text[:300])
        else:
            log.info("started ingest job execution")
    except Exception:
        log.exception("starting ingest job failed")


def start_ingest() -> None:
    if settings.ingest_job:
        threading.Thread(target=_run, args=(settings.ingest_job,), daemon=True).start()
