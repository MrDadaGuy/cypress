"""Who is who, and what each role may see. The single source of truth for access rules.

Roles (the people table; see people.py):
  admin     runs the Cypress console (content, models, users); board, CAM or a volunteer. Sees what the board sees
  board     LHCA board
  staff     paid or volunteer park staff (pool, clubhouse)
  security  park security / patrol
  member    a verified LHCA member (signed in)
  (none)    the public: anonymous residents and visitors

Document-set visibility (who can retrieve a set's documents):
  public    everyone
  members   signed-in members and everyone above
  staff     staff and board
  security  security and board
  board     board only

No role can ever see other members' personal data through retrieval: member records are not
document sets. Member lookup is a separate, audited tool for board, staff and security only.
"""

ROLES = ("admin", "board", "staff", "security", "member")
VISIBILITIES = ("public", "members", "staff", "security", "board")

_SEES: dict[str | None, tuple[str, ...]] = {
    None: ("public",),
    "member": ("public", "members"),
    "staff": ("public", "members", "staff"),
    "security": ("public", "members", "security"),
    "board": VISIBILITIES,
    "admin": VISIBILITIES,
}

LABELS = {
    None: "a member of the public (anonymous)",
    "member": "a signed-in LHCA member",
    "staff": "signed-in LHCA park staff",
    "security": "signed-in LHCA park security",
    "board": "a signed-in LHCA board member",
    "admin": "a signed-in LHCA Cypress administrator",
}


def visible_to(role: str | None) -> list[str]:
    """Document-set visibilities this role may retrieve. Unknown roles get public only."""
    return list(_SEES.get(role, _SEES[None]))


# Membership data (households, names, vehicles, gate cards, golf carts) is for these roles only, in chat and
# Slack, at the level each role's admin setting allows. Fixed in code: no setting can extend it to staff,
# members or the public.
MEMBER_DATA_ROLES = ("admin", "board", "security")


def can_lookup_members(role: str | None) -> bool:
    return role in MEMBER_DATA_ROLES


def label(role: str | None) -> str:
    return LABELS.get(role, LABELS[None])


def common_role(roles: list[str | None]) -> str | None:
    """The role whose access is exactly what *all* of these roles share: used when an answer is seen by
    a group (a Slack channel). Visibility sets intersect to one of the existing roles' sets, e.g.
    board + security -> security, staff + security -> member, anyone public -> public."""
    shared = set(VISIBILITIES)
    for r in roles:
        shared &= set(_SEES.get(r, _SEES[None]))
    for r in (None, "member", "staff", "security", "board"):
        if set(_SEES[r]) == shared:
            return r
    return None
