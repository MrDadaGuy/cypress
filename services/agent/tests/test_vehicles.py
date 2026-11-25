"""Vehicles: CSV import and household matching, phones on households, and plate lookup by role."""

import json

import pytest

from cypress import member_lookup as ml
from cypress import org, vehicles
from cypress.db import tx

MEMBERS = b"""Household,Address,Account #,Email,Phone,Status
Rivera,123 Example Lane,A1,jr@rivera.test,(512) 555-0101,Active
Okafor,9 Sample Ct,A2,co@okafor.test,,Lapsed
"""
CARS = b"""Plate,State,Make,Model,Color,Year,Sticker #,Account,Address
zzt-1234,Texas,Honda,CR-V,Blue,2019,P-101,A1,
ZZK 5678,TX,Ford,F-150,White,2015,,,9 Sample Court
ZZQ9999,TX,Tesla,Model 3,Red,2023,,,500 Elsewhere Rd
,TX,Kia,Soul,Green,2020,,A1,
ZZT1234,TX,Honda,CR-V,Blue,2020,P-101,A1,
"""


@pytest.fixture
def data(client):
    with tx() as conn:
        conn.execute("delete from vehicles")
        conn.execute("delete from households")
        conn.execute("delete from member_lookups")
        conn.execute("delete from rate_events where key like 'lookup:%%'")
        conn.execute("delete from org_settings where key = 'member_lookup'")
    org.clear()
    _post(client, "members", MEMBERS)
    yield
    with tx() as conn:
        conn.execute("delete from vehicles")


def _post(client, what, body, replace=False):
    p = client.post(f"/api/admin/{what}/import/preview", files={"file": ("x.csv", body, "text/csv")}).json()
    r = client.post(f"/api/admin/{what}/import", files={"file": ("x.csv", body, "text/csv")},
                    data={"mapping": json.dumps(p["mapping"]), "replace": str(replace).lower()}).json()
    return p, r


def test_plate_and_state_normalization():
    assert vehicles.norm_plate("zzt-1234") == vehicles.norm_plate("ZZT 1234") == "ZZT1234"
    assert vehicles.norm_state("Texas") == vehicles.norm_state("tx") == "TX" and vehicles.norm_state("Narnia") == ""


def test_member_import_keeps_phone_numbers(data, client):
    h = client.get("/api/admin/members?q=rivera").json()["households"][0]
    assert h["phones"] == ["5125550101"]


def test_vehicle_import_matches_households(data, client):
    p, r = _post(client, "vehicles", CARS)
    assert p["mapping"]["plate"] == "Plate" and p["mapping"]["tag"] == "Sticker #" and p["mapping"]["account_ref"] == "Account"
    assert p["vehicles"] == 3 and p["matched"] == 2 and p["unmatched"] == 1
    assert any("no plate" in x for x in p["problems"]) and any("also on line" in x for x in p["problems"])
    assert any("didn't match a household" in x for x in p["problems"])
    assert r["created"] == 3 and r["unmatched"] == 1
    listing = client.get("/api/admin/vehicles?q=zzt").json()["vehicles"]
    assert len(listing) == 1 and listing[0]["household"] == "Rivera" and listing[0]["year"] == 2020   # later line won
    # Matched by address ("Court" vs "Ct") when there's no account number.
    assert client.get("/api/admin/vehicles?q=ford").json()["vehicles"][0]["household"] == "Okafor"
    # Replace removes cars that aren't in the new file.
    _, r2 = _post(client, "vehicles", b"Plate,State,Account\nZZT1234,TX,A1\n", replace=True)
    assert r2["updated"] == 1 and r2["removed"] == 2


def test_member_import_links_vehicles_that_now_match(data, client):
    _post(client, "vehicles", CARS)
    _, r = _post(client, "members", b"Household,Address,Email\nNew,500 Elsewhere Rd,n@new.test\n")
    assert r["vehicles_linked"] == 1


def plate(p, role="security", **kw):
    return ml.lookup_plate(plate=p, state=kw.get("state"), role=role, actor="guard@x.test", channel="web")


def test_plate_lookup_confirm_never_reveals_the_household(data, client):
    _post(client, "vehicles", CARS)
    r = plate("ZZT-1234")
    assert r["mode"] == "confirm" and "active member household" in r["say"] and "Honda" in r["say"]
    blob = json.dumps(r)
    assert "Rivera" not in blob and "Example Lane" not in blob
    r = plate("ZZK5678")                                          # lapsed household
    assert "not for an active member household" in r["say"]
    assert "No registered vehicle" in plate("ZZX0000")["say"]
    r = plate("ZZ?1234")                                          # partial
    assert r["partial"] and "could match" in r["say"] and "ZZT1234" not in json.dumps(r)
    assert "error" in plate("ZZ")                                 # too short to search


def test_plate_lookup_details_for_board(data, client):
    _post(client, "vehicles", CARS)
    m = plate("zzt 1234", role="board")["matches"][0]
    assert m["household"] == "Rivera" and m["address"] == "123 Example Lane" and m["exact"] and m["tag"] == "P-101"
    p = plate("ZZT12", role="board")
    assert p["partial"] and p["matches"][0]["plate"].upper().replace("-", "") == "ZZT1234"
    assert "error" in plate("ZZT1234", role="member")
    with tx() as conn:
        rows = conn.execute("select plate_q, mode from member_lookups order by id").fetchall()
    assert [r["plate_q"] for r in rows] == ["zzt 1234", "ZZT12"] and rows[0]["mode"] == "details"


def test_plate_lookup_dry_run_uses_fictional_vehicles():
    r = ml.lookup_plate(plate="ZZT1234", role="board", actor="eval", channel="eval", dry_run=True)
    assert r["matches"][0]["household"] == "Rivera" and r["matches"][0]["car"] == "blue 2019 Honda CR-V"


def test_member_data_is_board_admin_and_security_only(data, client):
    from cypress import roles
    from cypress.agent import tools
    assert roles.MEMBER_DATA_ROLES == ("admin", "board", "security")
    for role in ("staff", "member", None):
        assert "error" in plate("ZZT1234", role=role)
        assert "error" in ml.lookup_card(kind="gate_card", number="1234567", role=role, actor="x", channel="web")
        assert not {"lookup_member", "lookup_plate", "lookup_card"} & {t.name for t in tools.for_role(role)}
    # No setting can give staff access.
    r = client.put("/api/admin/member-lookup", json={"access": {"staff": "details", "board": "details"}, "per_hour": 30, "per_day": 100})
    assert r.status_code == 422


def test_card_lookup_by_role():
    kw = dict(actor="eval", channel="eval", dry_run=True)
    c = ml.lookup_card(kind="gate_card", number="1234567", role="security", **kw)
    assert c["mode"] == "confirm" and "active member household" in c["say"] and "Rivera" not in json.dumps(c)
    v = ml.lookup_card(kind="gate_card", number="5550001", role="security", **kw)
    assert "vendor card (Example Pools LLC)" in v["say"]
    s = ml.lookup_card(kind="sticker", number="101", role="security", **kw)     # leading zeros don't matter
    assert s["describes"] == "blue 2019 Honda CR-V"
    d = ml.lookup_card(kind="golf_tag", number="7000001", role="board", **kw)
    assert d["matches"][0]["household"] == "Rivera" and d["matches"][0]["address"] == "123 Example Lane"
    assert "isn't on file" in ml.lookup_card(kind="gate_card", number="9999999", role="security", **kw)["say"]
