"""CAM's membership workbook: tabs recognized by headers, colors captured and grouped, the legend's effects,
what-changed alerts, the emptied-file guard, and the Drive sync. All data here is fictional."""

import io

import pytest
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from cypress import org, sheet_sync, workbook
from cypress.db import tx

BLUE, BLUE2, PEACH, YELLOW, GREEN = "DBE5F1", "DCE6F1", "FBD4B4", "FFFF00", "EAF1DD"
HEAD = ["Vantaca ", None, None, " Address", None, " Veh 1 Lic #", " Vehicle 1", None, " Veh 2 Lic #", " Vehicle 2",
        None, " Veh 3 Lic #", " Vehicle 3", " Gate Card 1", " Gate Card 2", None]
MEMBERS = [
    ["LHCA10001", "RIVERA", "Jordan & Sam Rivera", "123 Example Lane", "0101", "ZZT1234", "2019 HONDA CR-V-verified",
     "0102", "ZZT1299", "2021 TOYOTA CAMRY", None, None, None, "1234567", "1234568", None],
    ["LHCA10002", "CHEN", "Alex Chen", "456 Example Lane", "0201", "ZZK5678", "2015 FORD F-150", None, None, None,
     None, None, None, "2234567", None, "called about card"],
    ["LHCA10003", "PATEL", "Priya Patel", "789 Sample Rd", "0301", "ZZP0001", "2020 KIA SOUL", None, None, None,
     None, None, None, "3234567", None, None],
    ["LHCA10004", "NGUYEN", "Lee Nguyen", "12 Sample Ct", "0401", "ZZN0002", "2018 SUBARU OUTBACK", None, None, None,
     None, None, None, "4234567", None, None],
]


def fill(c, hexcolor):
    c.fill = PatternFill("solid", fgColor="FF" + hexcolor)


def build(members=None, extra_color=None, rename=None, extra_tab=False) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "LHPA Membership"
    ws.append(["2026 MEMBERSHIP"])
    ws.append([rename.get(h, h) if rename and h else h for h in HEAD])
    for i, row in enumerate(members if members is not None else MEMBERS):
        ws.append(row)
        r = ws.max_row
        if i == 0:                                       # light blue row (two near-identical shades)
            for col in range(1, 5):
                fill(ws.cell(r, col), BLUE if col < 3 else BLUE2)
            fill(ws.cell(r, 3), YELLOW)                  # yellow names cell
        if i == 1:
            for col in range(1, 5):
                fill(ws.cell(r, col), PEACH)            # peach row
            ws.cell(r, 16).font = Font(color="FFFF0000")  # red text note
        for col in (5, 6, 7):
            fill(ws.cell(r, col), GREEN)                # pale green vehicle block
        if extra_color and i == 2:
            fill(ws.cell(r, 4), extra_color)
    nr = wb.create_sheet("Non Renewal")
    nr.append(["NON RENEWALS 2026"])
    nr.append(["LHCA19001", "OKAFOR", "Chris Okafor", "9 Sample Ct", "0901", "ZZO0009", "2010 LEXUS RX 350", None, None,
               None, None, None, None, "9234567", None, "sold home"])
    g = wb.create_sheet("Golf Tags")
    g.append(["GOLF TAG", "Vantaca#", "Last Names ", "Members Names ", "Property Address ", "CART INFORMATION", "SERIAL", "WAIVER", "INACTIVE "])
    g.append(["7000001", "LHCA10001", "RIVERA", "Jordan Rivera", "123 Example Lane", "2019 EZGO RXV", "EZ123", "YES", None])
    g.append(["7000002", "LHCA99999", "NOBODY", "No Body", "1 Nowhere", "2010 CLUB CAR", "CC1", "YES", "mailed tag"])
    v = wb.create_sheet("Vendors")
    v.append(["NAME", "ADDRESS", "New Cards Issued 6/1/12", None, None])
    v.append(["Example Pools LLC", "PO Box 1", "5550001", "2024-07-03", "given all access"])
    u = wb.create_sheet("Unassigned Batch")
    u.append(["Purchased 2015"])
    u.append(["5550099"])
    if extra_tab:
        wb.create_sheet("Boat Slips").append(["SLIP", "Vantaca#"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def clean(client):
    with tx() as conn:
        for t in ("vehicles", "gate_cards", "golf_carts", "households", "member_sync_runs", "workbook_snapshots"):
            conn.execute(f"delete from {t}")
        conn.execute("update org_settings set value = '{\"colors\": {}}' where key = 'member_colors'")
        conn.execute("delete from org_settings where key = 'member_sync'")
    org.clear()
    yield
    with tx() as conn:
        conn.execute("update org_settings set value = '{\"colors\": {}}' where key = 'member_colors'")
    org.clear()


def upload(client, data, what="import"):
    return client.post(f"/api/admin/members/workbook/{what}", files={"file": ("m.xlsx", data, "application/octet-stream")}).json()


def test_tabs_are_recognized_by_their_headers(clean, client):
    p = upload(client, build(), "preview")
    kinds = {t["name"]: t["kind"] for t in p["tabs"]}
    assert kinds == {"LHPA Membership": "households", "Non Renewal": "former", "Golf Tags": "golf_carts",
                     "Vendors": "vendors", "Unassigned Batch": "ignore"}
    cols = next(t for t in p["tabs"] if t["name"] == "LHPA Membership")["columns"]
    assert cols["household"] == "column B" and cols["people"] == "column C" and cols["sticker_1"] == "column E"
    assert p["counts"] == {"households": 4, "inactive": 1, "people": 6, "vehicles": 6, "gate_cards": 6, "golf_carts": 2,
                           "vendor_cards": 1, "golf_carts_unmatched": 1, "contacts": 0, "emails": 0}
    blue = next(c for c in p["colors"] if c["hex"] == BLUE)
    assert BLUE2 in blue["aliases"] and blue["name"] == "light blue"         # near-identical shades grouped
    assert any("New color" in c["what"] for c in p["changes"])


def test_import_captures_households_vehicles_cards_carts_and_colors(clean, client):
    r = upload(client, build())
    assert r["stats"]["households"]["created"] == 5 and r["stats"]["golf_carts"]["unmatched"] == 1
    with tx() as conn:
        h = conn.execute("select * from households where account_ref = 'LHCA10001'").fetchone()
        assert h["people"] == ["Jordan", "Sam Rivera"] and h["row_color"] == BLUE and h["status"] == "active"
        assert {"field": "people", "color": YELLOW, "kind": "fill"} in h["highlights"]
        chen = conn.execute("select * from households where account_ref = 'LHCA10002'").fetchone()
        assert chen["row_color"] == PEACH and {"field": "notes", "color": "FF0000", "kind": "text"} in chen["highlights"]
        assert conn.execute("select status from households where account_ref = 'LHCA19001'").fetchone()["status"] == "inactive"
        car = conn.execute("select * from vehicles where plate_norm = 'ZZT1234'").fetchone()
        assert (car["year"], car["make"], car["model"], car["notes"], car["tag"]) == (2019, "Honda", "CR-V", "verified", "0101")
        assert car["household_id"] == h["id"] and car["highlights"][0]["color"] == GREEN
        assert conn.execute("select count(*) as n from gate_cards where household_id = %s", (h["id"],)).fetchone()["n"] == 2
        assert conn.execute("select holder from gate_cards where kind = 'vendor'").fetchone()["holder"] == "Example Pools LLC"
        cart = conn.execute("select * from golf_carts where tag = '7000001'").fetchone()
        assert cart["household_id"] == h["id"] and cart["waiver"] is True
    legend = workbook.legend()
    assert {BLUE, PEACH, YELLOW, GREEN, "FF0000"} <= set(legend) and legend[BLUE]["meaning"] is None


def test_legend_effects_set_type_and_status(clean, client):
    upload(client, build())
    colors = {k: {"name": v["name"], "meaning": v.get("meaning"), "effect": v.get("effect")} for k, v in workbook.legend().items()}
    colors[PEACH] = {"name": "peach", "meaning": "Senior member", "effect": "type:senior"}
    colors[BLUE] = {"name": "light blue", "meaning": "Renewed late", "effect": None}
    assert client.put("/api/admin/member-colors", json={"colors": colors}).status_code == 200
    bad = {**colors, PEACH: {**colors[PEACH], "effect": "type:astronaut"}}
    assert client.put("/api/admin/member-colors", json={"colors": bad}).status_code == 422
    upload(client, build())
    with tx() as conn:
        assert conn.execute("select member_type from households where account_ref = 'LHCA10002'").fetchone()["member_type"] == "senior"
        assert conn.execute("select member_type from households where account_ref = 'LHCA10003'").fetchone()["member_type"] == "full"


def test_changes_are_reported(clean, client):
    upload(client, build())
    r = upload(client, build(extra_color="00FFFF", rename={" Gate Card 2": " Gate Card B"}, extra_tab=True))
    what = " | ".join(c["what"] for c in r["changes"])
    assert "New color cyan" in what and "Columns changed on \"LHPA Membership\"" in what and "New tab \"Boat Slips\"" in what
    assert r["run"]["status"] == "warning"
    # Nothing new the third time: no alerts.
    r = upload(client, build(extra_color="00FFFF", rename={" Gate Card 2": " Gate Card B"}, extra_tab=True))
    assert not [c for c in r["changes"] if c["level"] == "alert"]


def test_emptied_workbook_deactivates_nobody(clean, client):
    upload(client, build())
    r = upload(client, build(members=MEMBERS[:1]))
    assert r["warnings"] and r["stats"]["households"]["deactivated"] == 0
    with tx() as conn:
        assert conn.execute("select count(*) as n from households where status = 'active'").fetchone()["n"] == 4


def test_drive_sync(clean, client, monkeypatch):
    monkeypatch.setattr(sheet_sync, "fetch", lambda fid: (build(), {"name": "2026 LHCA Membership", "modifiedTime": "2026-11-24"}))
    link = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit?usp=sharing"
    assert client.post("/api/admin/member-sync/check", json={"file": link}).json()["file_name"] == "2026 LHCA Membership"
    s = client.put("/api/admin/member-sync", json={"file": link, "enabled": True}).json()["settings"]
    assert s["file_id"] == "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789" and s["file_name"] == "2026 LHCA Membership"
    run = client.post("/api/admin/member-sync/run").json()["runs"][0]
    assert run["trigger"] == "console" and run["stats"]["households"]["created"] == 5

    def denied(fid):
        raise sheet_sync.SheetError(f"Cypress can't open that file. Share it with {sheet_sync.service_account()} as a Viewer.")
    monkeypatch.setattr(sheet_sync, "fetch", denied)
    r = sheet_sync.run("nightly")
    assert r["status"] == "failed" and "Share it with" in r["error"]
    from cypress import digest
    assert "sync failed" in digest.sync_blocks(r)[0]["text"]["text"]
    assert sheet_sync.file_id("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/view") == "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def test_member_names_from_the_workbook_reach_member_lookup(clean, client):
    upload(client, build())
    from cypress import member_lookup as ml
    r = ml.lookup(address="123 Example Lane", name="Sam Rivera", role="security", actor="g@x.test", channel="web")
    assert r["active_member_household"] is True


def contacts_file(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Emails"
    ws.append(["Vantaca Account", "Owner Name", "Property Address", "Primary Email", "Secondary Email", "Cell Phone"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_contacts_file_links_emails_and_touches_nothing_else(clean, client):
    upload(client, build())
    data = contacts_file([["LHCA10001", "Jordan Rivera", "123 Example Lane", "Jordan@Rivera.test", "sam@rivera.test; extra@rivera.test", "(512) 555-0101"],
                          [None, "Alex Chen", "456 Example Lane", "alex@chen.test", None, None],
                          ["LHCA55555", "Nobody", "1 Nowhere", "nobody@x.test", None, None]])
    p = upload(client, data, "preview")
    assert [t["kind"] for t in p["tabs"]] == ["contacts"] and p["counts"]["emails"] == 5
    assert not [c for c in p["changes"] if "is gone" in c["what"]]          # compared with contacts files, not the workbook
    r = upload(client, data)
    assert r["stats"]["contacts"] == {"households": 2, "emails": 4, "phones": 1, "unmatched": 1, "shared": 0}
    assert "households" not in r["stats"] and not r["warnings"]
    with tx() as conn:
        rivera = conn.execute("select id, phones from households where account_ref = 'LHCA10001'").fetchone()
        emails = {e["email"] for e in conn.execute("select email from member_emails where household_id = %s", (rivera["id"],))}
        assert emails == {"jordan@rivera.test", "sam@rivera.test", "extra@rivera.test"} and rivera["phones"] == ["5125550101"]
        assert conn.execute("select count(*) as n from households where status = 'active'").fetchone()["n"] == 4
        assert conn.execute("select count(*) as n from vehicles").fetchone()["n"] == 6


def test_email_columns_on_the_households_tab(clean, client):
    rows = [r + ["pat@example.test"] for r in MEMBERS]
    wb_bytes = build(members=rows)
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(wb_bytes))
    wb["LHPA Membership"].cell(2, 17).value = "Email"
    buf = io.BytesIO()
    wb.save(buf)
    rows[1][16] = "alex@chen.test"
    r = upload(client, buf.getvalue())
    assert r["stats"]["contacts"]["emails"] >= 1


def test_vantaca_style_labeled_emails_and_shared_addresses(clean, client):
    upload(client, build())
    wb = Workbook()
    ws = wb.active
    ws.append(["Account #", "Homeowner", "Address", "All Emails", "Balance"])
    ws.append(["LHCA10001", "Jordan & Sam Rivera", "123 Example Lane, Austin, TX 78734",
               "Jordan Rivera: jordan@rivera.test,  Sam Rivera: sam@rivera.test", 0])
    ws.append(["LHCA10002", "Alex Chen", "456 Example Lane", "Alex: alex@chen.test, Rivera: jordan@rivera.test", 125])
    buf = io.BytesIO()
    wb.save(buf)
    r = upload(client, buf.getvalue())
    assert r["stats"]["contacts"]["shared"] == 1 and r["stats"]["contacts"]["emails"] == 3
    with tx() as conn:
        rows = {e["email"]: e for e in conn.execute(
            "select e.email, e.name, h.account_ref from member_emails e join households h on h.id = e.household_id")}
        assert rows["jordan@rivera.test"]["account_ref"] == "LHCA10001" and rows["sam@rivera.test"]["name"] == "Sam Rivera"
        assert rows["alex@chen.test"]["name"] == "Alex"
        # The balance is stored (for later) but nothing reads it.
        assert conn.execute("select dues_balance from households where account_ref = 'LHCA10002'").fetchone()["dues_balance"] == 125
