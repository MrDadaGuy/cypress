from psycopg import Connection
from psycopg.types.json import Jsonb

from .auth import User


def audit(conn: Connection, user: User, action: str, entity: str, entity_id, detail=None) -> None:
    """Record an admin action in the same transaction as the change itself."""
    conn.execute(
        "insert into audit_log (actor, action, entity, entity_id, detail) values (%s, %s, %s, %s, %s)",
        (user.email, action, entity, None if entity_id is None else str(entity_id), Jsonb(detail or {})),
    )
