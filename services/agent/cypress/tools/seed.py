"""Create the starter document sets if they don't exist, and queue their first runs.

Used once per environment:  python -m cypress.ingest.worker --once --seed
"""

from psycopg.types.json import Jsonb

from ..db import tx

SEED = [
    {
        "slug": "lhca-website", "name": "LHCA website", "visibility": "public", "doc_type": "general",
        "authority_rank": 50,
        "description": "Pages and PDFs on lakehillsca.org: park hours and rules, pool, lap swimming, kayak racks, "
                       "clubhouse rental, membership and dues, FAQs, forms and waivers, and the current bylaws.",
        "sources": [{"kind": "web", "url": "https://lakehillsca.org/", "label": "lakehillsca.org",
                     "config": {"max_depth": 2, "max_pages": 200, "allow_domains": ["lakehillsca.org"],
                                "include": [], "exclude": [], "follow_pdfs": True, "respect_robots": True},
                     "schedule": "weekly"}],
    },
]


def seed(actor: str = "seed") -> list[str]:
    created = []
    with tx() as conn:
        for s in SEED:
            if conn.execute("select 1 from document_sets where slug = %s", (s["slug"],)).fetchone():
                continue
            set_id = conn.execute(
                """insert into document_sets (slug, name, description, visibility, doc_type, authority_rank, created_by)
                   values (%s, %s, %s, %s, %s, %s, %s) returning id""",
                (s["slug"], s["name"], s["description"], s["visibility"], s["doc_type"], s["authority_rank"], actor),
            ).fetchone()["id"]
            for src in s["sources"]:
                sid = conn.execute(
                    """insert into sources (set_id, kind, url, label, config, schedule, created_by)
                       values (%s, %s, %s, %s, %s, %s, %s) returning id""",
                    (set_id, src["kind"], src["url"], src["label"], Jsonb(src["config"]), src["schedule"], actor),
                ).fetchone()["id"]
                conn.execute("insert into jobs (set_id, source_id, triggered_by) values (%s, %s, %s)", (set_id, sid, actor))
            conn.execute(
                "insert into audit_log (actor, action, entity, entity_id, detail) values (%s, 'seed', 'document_set', %s, %s)",
                (actor, str(set_id), Jsonb({"slug": s["slug"]})),
            )
            created.append(s["slug"])
    return created
