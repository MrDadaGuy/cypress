"""Postgres connection pool and a tiny forward-only migration runner."""

import logging
from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

log = logging.getLogger(__name__)
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

# No server-side prepared statements: a migration that changes a table would otherwise break cached plans
# ("cached plan must not change result type") on long-lived connections. Negligible cost at this scale.
pool = ConnectionPool(
    settings.database_url, min_size=1, max_size=5, open=False,
    kwargs={"row_factory": dict_row, "prepare_threshold": None}
)


@contextmanager
def tx():
    """A connection inside a transaction: commits on success, rolls back on error."""
    with pool.connection() as conn, conn.transaction():
        yield conn


def migrate() -> list[str]:
    applied = []
    with tx() as conn:
        conn.execute(
            "create table if not exists schema_migrations"
            " (name text primary key, applied_at timestamptz not null default now())"
        )
        # Serialize concurrent starts (several Cloud Run instances booting at once).
        conn.execute("select pg_advisory_xact_lock(7243001)")
        done = {r["name"] for r in conn.execute("select name from schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            log.info("applying migration %s", path.name)
            conn.execute(path.read_text())
            conn.execute("insert into schema_migrations (name) values (%s)", (path.name,))
            applied.append(path.name)
    return applied
