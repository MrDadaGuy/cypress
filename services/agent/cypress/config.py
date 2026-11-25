import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    env: str = os.getenv("CYPRESS_ENV", "dev")
    project: str = os.getenv("GCP_PROJECT", "lhca-cypress")
    database_url: str = os.getenv("DATABASE_URL", "postgresql:///cypress")
    # Local development only: treat every request as this signed-in admin.
    # Ignored unless CYPRESS_ENV=dev, so it can never leak into production.
    dev_user: str | None = os.getenv("CYPRESS_DEV_USER")
    # Public contact for residents and for the crawler User-Agent. Not a GCP principal.
    support_email: str = os.getenv("CYPRESS_SUPPORT_EMAIL", "LHCA.votes@gmail.com")
    # Full name of the Cloud Run ingest job; when set, queuing work starts an execution.
    ingest_job: str | None = os.getenv("CYPRESS_INGEST_JOB")
    # Slack over HTTP (Events API). Tokens come from Secret Manager via Cloud Run secret env vars.
    slack_bot_token: str | None = os.getenv("SLACK_BOT_TOKEN")
    slack_signing_secret: str | None = os.getenv("SLACK_SIGNING_SECRET")
    # Cloud Tasks queue that hands Slack questions back to this service; unset = run in-process (dev).
    slack_queue: str | None = os.getenv("CYPRESS_SLACK_QUEUE")
    eval_queue: str | None = os.getenv("CYPRESS_EVAL_QUEUE")
    self_url: str | None = os.getenv("CYPRESS_SELF_URL")       # this service's URL (task target + OIDC audience)
    run_sa: str | None = os.getenv("CYPRESS_RUN_SA")           # identity Cloud Tasks signs as
    # Rust tickets service. Locally http://localhost:8081; on Cloud Run its https URL (IAM-protected).
    tickets_url: str = os.getenv("CYPRESS_TICKETS_URL", "http://localhost:8081")
    # Private bucket for photos; unset in dev = files under .uploads/ next to the service.
    uploads_bucket: str | None = os.getenv("CYPRESS_UPLOADS")
    cors_origins: tuple[str, ...] = tuple(
        o for o in os.getenv("CYPRESS_CORS_ORIGINS", "http://localhost:5173").split(",") if o
    )


settings = Settings()
