"""Grant or remove a Cypress role from the command line (bootstrap; the console's Users & roles
page is the normal way).

    DATABASE_URL=... .venv/bin/python -m cypress.tools.set_role someone@example.com admin
    DATABASE_URL=... .venv/bin/python -m cypress.tools.set_role someone@example.com none

Takes effect within a minute; no need to sign out.
"""

import sys

from .. import roles
from ..db import pool, tx

ROLES = (*roles.ROLES, "none")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[2] not in ROLES:
        sys.exit(f"usage: set_role EMAIL {{{'|'.join(ROLES)}}}")
    email, role = sys.argv[1].lower(), (None if sys.argv[2] == "none" else sys.argv[2])
    pool.open()
    with tx() as conn:
        conn.execute("""insert into people (email, role, created_by) values (%s, %s, 'set_role')
                        on conflict (email) do update set role = excluded.role, updated_by = 'set_role', updated_at = now()""",
                     (email, role))
        conn.execute("insert into audit_log (actor, action, entity, entity_id, detail) values ('set_role', 'update', 'person', %s, null)",
                     (email,))
    print(f"{email}: role = {role}")


if __name__ == "__main__":
    main()
