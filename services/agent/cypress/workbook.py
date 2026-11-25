"""CAM's membership workbook: parse, recognize tabs, import, and say what changed.

The workbook (an .xlsx, or a Google Sheet exported to .xlsx) is the record CAM keeps alongside Vantaca. Tabs are
recognized by their headers, not their names, so next year's file works without setup:
  households    a header row with "Veh 1 Lic #": account, last name, member names, address, up to three vehicles
                (sticker #, plate, "2017 CHRYSLER PACIFIC-verified"), gate cards, notes. Blank headers are placed by
                position next to the named ones.
  former        no header row, but rows shaped like the households tab (account numbers like LHCA12345 in
                the first column), e.g. "Non Renewal": imported as inactive households.
  golf_carts    "GOLF TAG" and "SERIAL" headers.
  vendors       "NAME" and "New Cards Issued…" headers: gate cards issued to vendors and contractors.
  contacts      an email column plus an account number or address (a tab, or a separate contacts file): emails
                (and phones) linked to existing households by account, else address. Emails are never removed by
                an import, since members may already sign in with them. A households tab with email columns works too.
  ignore        anything else (listed, not imported).
An admin can override any tab's kind (org_settings 'member_sync'.tab_kinds).

Colors are part of CAM's record. Each household keeps its row color and any field colored differently (and red
text); each vehicle its plate-cell color. What a color means is the admin's legend (org_settings 'member_colors'):
a name, a meaning, and an optional effect: a row color can mark the household inactive ("inactive") or set its
membership type ("type:senior"; the types are an admin list, org_settings 'member_types'). Households with no
type from a color get the default type. Near-identical shades
are grouped. A color with no meaning yet is still recorded.

Every import is compared with the previous one and with the legend; anything new or different is reported
(new or missing tab, changed headers, a color not in the legend, a big change in row count) so the admin only
needs to check with CAM when something actually changed. Values are read in memory; files are never stored.
"""

import io
import logging
import re
from collections import Counter, defaultdict

from psycopg.types.json import Jsonb

from .db import tx

log = logging.getLogger(__name__)

PALETTE = {  # names for the legend; any hex gets the nearest name
    "FFFF00": "yellow", "FF0000": "red", "00FF00": "bright green", "00FFFF": "cyan", "DBE5F1": "light blue",
    "CFE2F3": "sky blue", "FBD4B4": "peach", "EAF1DD": "pale green", "CCC0D9": "lavender", "D3D3D3": "grey",
    "A5A5A5": "dark grey", "F2DBDB": "pink", "FCE5CD": "light orange", "FFF2CC": "light yellow", "D9EAD3": "mint",
    "F4CCCC": "light red", "000000": "black", "0000FF": "blue", "FF9900": "orange", "9900FF": "purple",
}
SAME_SHADE = 6             # RGB distance under which two shades count as the same color (hand-picked near-duplicates differ by ~2)
ROW_CHANGE = 0.15          # report a tab whose row count moved more than this
ACCOUNT = re.compile(r"^LHCA\d{3,}$", re.I)
LABELS = {"account_ref": "account", "household": "last name", "people": "member names", "gate": "gate card",
          "holder": "vendor", "tag": "tag", "status_note": "inactive note", "description": "cart"}
DEFAULT_TEXT = {"000000", "202124", "242424", "1F1F1F", "333333"}


# ---------------- parsing ----------------

def _hex(color) -> str | None:
    """openpyxl color -> 'RRGGBB', or None for no color / white / automatic."""
    if color is None:
        return None
    if color.type == "rgb" and isinstance(color.rgb, str) and len(color.rgb) >= 6:
        h = color.rgb[-6:].upper()
        return None if h in ("FFFFFF",) or color.rgb in ("00000000",) else h
    if color.type == "theme":
        return None if color.theme in (0, 1) and not color.tint else f"THEME{color.theme}"
    if color.type == "indexed" and color.indexed not in (64, 65, 9, 1):
        return f"INDEXED{color.indexed}"
    return None


def parse(data: bytes) -> list[dict]:
    """[{name, rows: [[{v, fill, text}]]}] with empty rows dropped."""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = []
        for r in ws.iter_rows(max_col=40):
            cells = []
            for c in r:
                v = c.value
                v = "" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)).strip()
                fill = _hex(c.fill.fgColor) if c.fill is not None and c.fill.fill_type not in (None, "none") else None
                text = _hex(c.font.color) if c.font is not None and c.font.color is not None else None
                cells.append({"v": v, "fill": fill, "text": None if text in DEFAULT_TEXT else text})
            while cells and not cells[-1]["v"] and not cells[-1]["fill"]:
                cells.pop()
            if any(x["v"] for x in cells):
                rows.append(cells)
        out.append({"name": ws.title, "rows": rows})
    return out


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower())


# ---------------- recognizing tabs ----------------

def _household_cols(header: list[str]) -> dict:
    """Column indexes for a households header row; blank headers placed next to the named ones."""
    h = [_norm(x) for x in header]
    cols: dict = {}
    for i, x in enumerate(h):
        if x.startswith("vantaca"):
            cols["account_ref"] = i
        elif x == "address" or x.endswith(" address"):
            cols.setdefault("address", i)
        elif m := re.fullmatch(r"veh(?:icle)? ?(\d) lic.*", x):
            cols[f"plate_{m[1]}"] = i
        elif m := re.fullmatch(r"vehicle ?(\d)", x):
            cols[f"vehicle_{m[1]}"] = i
        elif m := re.fullmatch(r"gate card ?(\d)", x):
            cols[f"gate_{m[1]}"] = i
        elif x in ("notes", "note", "comments"):
            cols["notes"] = i
        elif "email" in x or "e-mail" in x:
            cols[f"email_{sum(k.startswith('email_') for k in cols) + 1}"] = i
        elif "phone" in x or x in ("cell", "mobile"):
            cols[f"phone_{sum(k.startswith('phone_') for k in cols) + 1}"] = i
    a = cols.get("account_ref", 0)
    cols.setdefault("account_ref", a)
    if not h[a + 1:a + 2] or not h[a + 1]:
        cols.setdefault("household", a + 1)
    if not h[a + 2:a + 3] or not h[a + 2]:
        cols.setdefault("people", a + 2)
    for n in (1, 2, 3):
        p = cols.get(f"plate_{n}")
        if p is not None and p > 0 and not h[p - 1]:
            cols[f"sticker_{n}"] = p - 1
    gates = [v for k, v in cols.items() if k.startswith("gate_")]
    if gates and "notes" not in cols:
        cols["notes"] = max(gates) + 1
    return cols


def _find_header(rows: list[list[dict]], test) -> int | None:
    for i, r in enumerate(rows[:6]):
        if test([_norm(c["v"]) for c in r]):
            return i
    return None


def classify(tabs: list[dict], overrides: dict | None = None) -> list[dict]:
    """Each tab with its kind, header row and column map."""
    overrides = overrides or {}
    out, house_cols = [], None
    for t in tabs:
        rows = t["rows"]
        info = {"name": t["name"], "rows": rows, "kind": "ignore", "header": None, "cols": {}, "headers": []}
        hi = _find_header(rows, lambda h: any(re.fullmatch(r"veh(?:icle)? ?1 lic.*", x) for x in h))
        if hi is not None:
            info.update(kind="households", header=hi, headers=[c["v"] for c in rows[hi]], cols=_household_cols([c["v"] for c in rows[hi]]))
            house_cols = house_cols or info["cols"]
        elif (hi := _find_header(rows, lambda h: "golf tag" in h and "serial" in h)) is not None:
            names = {_norm(c["v"]): i for i, c in enumerate(rows[hi])}
            pick = lambda *keys: next((names[k] for k in names if any(k.startswith(x) for x in keys)), None)
            info.update(kind="golf_carts", header=hi, headers=[c["v"] for c in rows[hi]],
                        cols={k: v for k, v in {"tag": pick("golf tag"), "account_ref": pick("vantaca"), "household": pick("last name"),
                                                 "people": pick("members name", "member name"), "address": pick("property address", "address"),
                                                 "description": pick("cart"), "serial": pick("serial"), "waiver": pick("waiver"),
                                                 "status_note": pick("inactive", "status")}.items() if v is not None})
        elif (hi := _find_header(rows, lambda h: "name" in h and any(x.startswith("new cards") or x.startswith("card") for x in h))) is not None:
            names = [_norm(c["v"]) for c in rows[hi]]
            card = next(i for i, x in enumerate(names) if x.startswith("new cards") or x.startswith("card"))
            info.update(kind="vendors", header=hi, headers=[c["v"] for c in rows[hi]],
                        cols={"holder": names.index("name"), "address": names.index("address") if "address" in names else None,
                              "card": card, "issued": card + 1, "notes": card + 2})
        elif (hi := _find_header(rows, lambda h: any("email" in x or "e-mail" in x for x in h)
                                 and any(x.startswith("vantaca") or "account" in x or "address" in x for x in h))) is not None:
            names = [_norm(c["v"]) for c in rows[hi]]
            cols: dict = {}
            for i, x in enumerate(names):
                if x.startswith("vantaca") or "account" in x:
                    cols.setdefault("account_ref", i)
                elif "address" in x and "email" not in x:
                    cols.setdefault("address", i)
                elif "email" in x or "e-mail" in x:
                    cols[f"email_{sum(k.startswith('email_') for k in cols) + 1}"] = i
                elif "phone" in x or x in ("cell", "mobile"):
                    cols[f"phone_{sum(k.startswith('phone_') for k in cols) + 1}"] = i
                elif x in ("balance", "dues balance", "account balance", "current balance"):
                    cols["balance"] = i
                elif "name" in x or x in ("owner", "owners", "member", "members", "homeowner", "homeowners"):
                    cols.setdefault("people", i)
            info.update(kind="contacts", header=hi, headers=[c["v"] for c in rows[hi]], cols=cols)
        else:
            data = [r for r in rows[:20] if sum(bool(x["v"]) for x in r) >= 3]     # skip title rows
            first = [r[0]["v"] for r in data if r]
            if first and sum(bool(ACCOUNT.match(x)) for x in first) >= max(1, len(first) * 0.6) and max(len(r) for r in data) >= 10:
                info.update(kind="former", header=-1)
        kind = overrides.get(t["name"])
        if kind and kind != info["kind"]:
            info["kind"], info["overridden"] = kind, True
            if kind in ("households", "former") and not info["cols"]:
                info["header"] = -1 if info["header"] is None else info["header"]
        out.append(info)
    for info in out:
        if info["kind"] == "former" and not info["cols"]:
            info["cols"] = house_cols or _household_cols(["Vantaca", "", "", "Address", "", "Veh 1 Lic #", "Vehicle 1", "",
                                                          "Veh 2 Lic #", "Vehicle 2", "", "Veh 3 Lic #", "Vehicle 3",
                                                          "Gate Card 1", "Gate Card 2"])
    return out


# ---------------- colors ----------------

def _rgb(h: str) -> tuple[int, int, int] | None:
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) if re.fullmatch(r"[0-9A-F]{6}", h or "") else None


def _dist(a: str, b: str) -> float:
    ra, rb = _rgb(a), _rgb(b)
    return 999.0 if not ra or not rb else sum((x - y) ** 2 for x, y in zip(ra, rb)) ** 0.5


def color_name(h: str) -> str:
    if not _rgb(h):
        return h.lower()
    return PALETTE[min(PALETTE, key=lambda p: _dist(h, p))]


def legend() -> dict:
    from . import org
    return (org.get().get("member_colors") or {}).get("colors") or {}


def canonical(h: str | None, colors: dict) -> str | None:
    """The legend key for a hex: itself, an alias, or a near-identical shade already in the legend."""
    if not h:
        return None
    if h in colors:
        return h
    for k, c in colors.items():
        if h in (c.get("aliases") or []):
            return k
    near = [k for k in colors if _dist(h, k) < SAME_SHADE]
    return min(near, key=lambda k: _dist(h, k)) if near else h


def with_shades(tabs: list[dict], colors: dict) -> dict:
    """The legend plus this workbook's colors, near-identical shades grouped under the most used one, so a
    first import (empty legend) still treats DBE5F1 and DCE6F1 as one color."""
    seen = Counter(x[k] for t in tabs for r in t["rows"] for x in r for k in ("fill", "text") if x[k] and x["v"])
    merged = {k: dict(v) for k, v in colors.items()}
    for h, _ in seen.most_common():
        k = canonical(h, merged)
        if k == h and h not in merged:
            merged[h] = {"name": color_name(h), "meaning": None, "effect": None, "new": True, "aliases": []}
        elif k != h and h not in (merged[k].get("aliases") or []):
            merged[k] = {**merged[k], "aliases": [*(merged[k].get("aliases") or []), h]}
    return merged


# ---------------- reading rows ----------------

def _vehicle_desc(s: str) -> dict:
    """'2017 CHRYSLER PACIFIC-daughter's verified' -> year 2017, make Chrysler, model PACIFIC, note "daughter's verified".
    A note starts at a hyphen followed by a lowercase letter, so model names like CR-V and F-150 stay whole."""
    s = (s or "").strip()
    m = re.search(r"\s*-\s*(?=[a-z])", s)
    body, note = (s[:m.start()], s[m.end():]) if m else (s, None)
    y = re.match(r"^(\d{4})\s+", body)
    year = int(y[1]) if y and 1900 <= int(y[1]) <= 2100 else None
    words = body[y.end():].split() if y else body.split()
    return {"year": year, "make": words[0].title() if words else None, "model": " ".join(words[1:]) or None,
            "notes": (note or "").strip() or None}


def _people(s: str) -> list[str]:
    parts = re.split(r"\s*(?:&|/|,|\band\b)\s*", s or "")
    return [p.strip() for p in parts if p.strip()][:6]


EMAIL = re.compile(r"[^@\s;,<>]+@[^@\s;,<>]+\.[^@\s;,<>]+")


LABELED = re.compile(r"(?:([^:,;@]{1,60}?)\s*:\s*)?(" + EMAIL.pattern + ")")


def _contact_bits(r, c) -> tuple[list[dict], list[str]]:
    """Emails (each with the name it's labeled with, as in Vantaca's "Pat: pat@x.com, Lee: lee@y.com") and phones."""
    emails, phones = [], []
    for k in sorted(c):
        x = _cell(r, c[k])["v"]
        if k.startswith("email_"):
            for m in LABELED.finditer(x):
                e = m.group(2).lower().rstrip(".")
                if e not in [y["email"] for y in emails]:
                    emails.append({"email": e, "name": (m.group(1) or "").strip(" -") or None})
        elif k.startswith("phone_"):
            d = re.sub(r"\D", "", x)
            d = d[1:] if len(d) == 11 and d.startswith("1") else d
            if len(d) == 10 and d not in phones:
                phones.append(d)
    return emails[:6], phones[:3]


def _money(v: str) -> float | None:
    s = re.sub(r"[$,\s]", "", v or "")
    neg = s.startswith("(") and s.endswith(")")
    try:
        n = float(s.strip("()"))
    except ValueError:
        return None
    return -n if neg else n


def _cell(row, i):
    return row[i] if i is not None and i < len(row) else {"v": "", "fill": None, "text": None}


def records(tabs: list[dict], colors: dict) -> dict:
    """Households, vehicles, gate cards, golf carts and vendor cards from classified tabs, with colors."""
    out = {"households": [], "vehicles": [], "gate_cards": [], "golf_carts": [], "vendor_cards": [], "contacts": []}
    key = lambda h: canonical(h, colors)
    for t in tabs:
        rows = t["rows"][(t["header"] + 1):] if t["kind"] in ("households", "golf_carts", "vendors") else t["rows"]
        c = t["cols"]
        if t["kind"] in ("households", "former"):
            for r in rows:
                get = lambda f: _cell(r, c.get(f))["v"]
                acct = get("account_ref")
                if not (acct or get("address")) or (t["kind"] == "former" and not (ACCOUNT.match(acct) or get("address"))):
                    continue
                ident = [_cell(r, c.get(f)) for f in ("account_ref", "household", "people", "address")]
                fills = Counter(key(x["fill"]) for x in ident if x["v"])
                row_color, n = fills.most_common(1)[0] if fills else (None, 0)
                row_color = row_color if n >= 2 else None
                hl = []
                for f in ("account_ref", "household", "people", "address", "gate_1", "gate_2", "notes"):
                    x = _cell(r, c.get(f))
                    if x["v"] and key(x["fill"]) and key(x["fill"]) != row_color:
                        hl.append({"field": f, "color": key(x["fill"]), "kind": "fill"})
                    if x["v"] and x["text"]:
                        hl.append({"field": f, "color": key(x["text"]), "kind": "text"})
                h = {"tab": t["name"], "account_ref": acct or None, "name": (get("household") or acct or get("address"))[:200],
                     "people": _people(get("people")), "address": get("address") or None, "notes": get("notes")[:1000] or None,
                     "status": "inactive" if t["kind"] == "former" else "active", "row_color": row_color, "highlights": hl}
                out["households"].append(h)
                ems, phs = _contact_bits(r, c)
                if ems or phs:
                    out["contacts"].append({"account_ref": acct or None, "address": h["address"], "emails": ems, "phones": phs,
                                            "people": h["people"]})
                for n in (1, 2, 3):
                    p = _cell(r, c.get(f"plate_{n}"))
                    if not p["v"]:
                        continue
                    from .vehicles import norm_plate
                    d = _vehicle_desc(get(f"vehicle_{n}"))
                    vh = [{"field": f, "color": key(x["fill"]), "kind": "fill"} for f, x in
                          (("plate", p), ("vehicle", _cell(r, c.get(f"vehicle_{n}")))) if x["v"] and key(x["fill"])]
                    vh += [{"field": "plate", "color": key(p["text"]), "kind": "text"}] if p["text"] else []
                    out["vehicles"].append({"account_ref": acct or None, "address": h["address"], "plate": p["v"][:20],
                                            "plate_norm": norm_plate(p["v"]), "tag": get(f"sticker_{n}")[:40] or None,
                                            **d, "highlights": vh, "household_status": h["status"]})
                for n in (1, 2):
                    g = get(f"gate_{n}")
                    if g:
                        out["gate_cards"].append({"card": g[:40], "account_ref": acct or None, "address": h["address"],
                                                  "color": key(_cell(r, c.get(f"gate_{n}"))["fill"])})
        elif t["kind"] == "golf_carts":
            for r in rows:
                get = lambda f: _cell(r, c.get(f))["v"]
                if not get("tag"):
                    continue
                w = get("waiver").lower()
                out["golf_carts"].append({"tag": get("tag")[:40], "account_ref": get("account_ref") or None, "address": get("address") or None,
                                          "description": get("description")[:200] or None, "serial": get("serial")[:80] or None,
                                          "waiver": True if w in ("yes", "y", "x") else (False if w in ("no", "n") else None),
                                          "status_note": get("status_note")[:200] or None,
                                          "color": key(_cell(r, c.get("tag"))["fill"]) or key(_cell(r, c.get("household"))["fill"])})
        elif t["kind"] == "contacts":
            for r in rows:
                acct, addr = _cell(r, c.get("account_ref"))["v"], _cell(r, c.get("address"))["v"]
                ems, phs = _contact_bits(r, c)
                bal = _money(_cell(r, c.get("balance"))["v"]) if "balance" in c else None
                if (acct or addr) and (ems or phs or bal is not None):
                    out["contacts"].append({"account_ref": acct or None, "address": addr or None, "emails": ems, "phones": phs,
                                            "people": _people(_cell(r, c.get("people"))["v"]), "balance": bal})
        elif t["kind"] == "vendors":
            for r in rows:
                get = lambda f: _cell(r, c.get(f))["v"]
                if not get("card"):
                    continue
                fills = Counter(key(x["fill"]) for x in r if x["v"])
                out["vendor_cards"].append({"card": get("card")[:40], "holder": get("holder")[:200] or None,
                                            "issued": get("issued")[:100] or None, "notes": get("notes")[:300] or None,
                                            "color": fills.most_common(1)[0][0] if fills else None})
    return out


# ---------------- shape and what changed ----------------

def shape(tabs: list[dict], colors: dict) -> dict:
    """A fingerprint of the workbook: tabs, kinds, headers, row counts, colors and where they're used."""
    used: dict = defaultdict(lambda: {"fills": 0, "text": 0, "where": Counter()})
    for t in tabs:
        names = {v: k for k, v in t["cols"].items() if v is not None}
        start = (t["header"] + 1) if t["header"] is not None and t["header"] >= 0 else 0
        for r in t["rows"][start:]:
            for i, x in enumerate(r):
                if not x["v"]:
                    continue
                for kind in ("fill", "text"):
                    k = canonical(x[kind], colors)
                    if k:
                        used[k]["fills" if kind == "fill" else "text"] += 1
                        field = re.sub(r"_\d$", "", names.get(i, f"column {_letter(i)}"))
                        used[k]["where"][f"{t['name']}: {LABELS.get(field, field)}"] += 1
    return {"family": family(tabs), "tabs": {t["name"]: {"kind": t["kind"], "headers": t["headers"], "rows": len(t["rows"]) - (t["header"] + 1 if t["header"] is not None and t["header"] >= 0 else 0)}
                     for t in tabs},
            "colors": {k: {"fills": v["fills"], "text": v["text"], "where": dict(v["where"].most_common(6))} for k, v in used.items()}}


def changes(new: dict, old: dict | None, colors: dict) -> list[dict]:
    """What's new or different: [{level: alert|info, what}]."""
    out = []
    for k, v in new["colors"].items():
        if canonical(k, colors) not in colors:
            out.append({"level": "alert", "what": f"New color {color_name(k)} (#{k}) on {v['fills'] + v['text']} cells "
                                                  f"({', '.join(v['where'])}). Set its meaning in the color legend."})
    for k, c in colors.items():
        if c.get("meaning") and k not in new["colors"]:
            out.append({"level": "info", "what": f"{c.get('name') or color_name(k)} ({c['meaning']}) no longer appears."})
    if old:
        for name, t in new["tabs"].items():
            o = old["tabs"].get(name)
            if not o:
                out.append({"level": "alert" if t["kind"] != "ignore" else "info",
                            "what": f"New tab \"{name}\" ({t['kind'].replace('_', ' ')}, {t['rows']} rows)."})
                continue
            if o["kind"] != t["kind"]:
                out.append({"level": "alert", "what": f"Tab \"{name}\" now looks like {t['kind']} (was {o['kind']})."})
            if [_norm(x) for x in o["headers"]] != [_norm(x) for x in t["headers"]]:
                out.append({"level": "alert", "what": f"Columns changed on \"{name}\": " + _header_diff(o["headers"], t["headers"])})
            if o["rows"] and abs(t["rows"] - o["rows"]) > ROW_CHANGE * o["rows"]:
                out.append({"level": "alert", "what": f"\"{name}\" went from {o['rows']} to {t['rows']} rows."})
        for name, o in old["tabs"].items():
            if name not in new["tabs"] and o["kind"] != "ignore":
                out.append({"level": "alert", "what": f"Tab \"{name}\" ({o['kind']}) is gone."})
    return out


def _header_diff(a: list[str], b: list[str]) -> str:
    na, nb = [_norm(x) for x in a], [_norm(x) for x in b]
    added = [x for x in b if _norm(x) and _norm(x) not in na]
    gone = [x for x in a if _norm(x) and _norm(x) not in nb]
    bits = ([f"added {', '.join(added)}"] if added else []) + ([f"removed {', '.join(gone)}"] if gone else [])
    return "; ".join(bits) or "columns moved or renamed"


def family(tabs: list[dict]) -> str:
    """Which kind of file this is, so a contacts file is compared with the last contacts file, not the workbook."""
    kinds = {t["kind"] for t in tabs}
    return "membership" if kinds & {"households", "former", "golf_carts", "vendors"} else ("contacts" if "contacts" in kinds else "other")


def last_shape(fam: str = "membership") -> dict | None:
    with tx() as conn:
        r = conn.execute("select shape from workbook_snapshots where coalesce(shape->>'family', 'membership') = %s "
                         "order by at desc limit 1", (fam,)).fetchone()
    return r["shape"] if r else None


# ---------------- import ----------------

def preview(data: bytes, overrides: dict | None = None) -> dict:
    """Everything an import would do, without saving."""
    saved = legend()
    tabs = classify(parse(data), overrides)
    colors = with_shades(tabs, saved)
    rec = records(tabs, colors)
    sh = shape(tabs, colors)
    matched = _account_index()
    return {"tabs": [{"name": t["name"], "kind": t["kind"], "rows": sh["tabs"][t["name"]]["rows"], "overridden": t.get("overridden", False),
                      "columns": {k: (t["headers"][v] if t["headers"] and v is not None and v < len(t["headers"]) and t["headers"][v] else
                                      (f"column {_letter(v)}" if v is not None else None)) for k, v in t["cols"].items()}}
                     for t in tabs],
            "counts": {"households": sum(h["status"] == "active" for h in rec["households"]),
                       "inactive": sum(h["status"] == "inactive" for h in rec["households"]),
                       "people": sum(len(h["people"]) for h in rec["households"]), "vehicles": len(rec["vehicles"]),
                       "gate_cards": len(rec["gate_cards"]), "golf_carts": len(rec["golf_carts"]),
                       "vendor_cards": len(rec["vendor_cards"]),
                       "contacts": len(rec["contacts"]), "emails": sum(len(x["emails"]) for x in rec["contacts"]),
                       "golf_carts_unmatched": sum(1 for g in rec["golf_carts"] if (g["account_ref"] or "").upper() not in matched
                                                   and (g["account_ref"] or "").upper() not in {(h["account_ref"] or "").upper() for h in rec["households"]})},
            "colors": [{"hex": k, "name": colors[k].get("name") or color_name(k), "meaning": colors[k].get("meaning"),
                        "aliases": colors[k].get("aliases") or [], "known": k in saved, **v}
                       for k, v in sorted(sh["colors"].items(), key=lambda x: -(x[1]["fills"] + x[1]["text"]))],
            "changes": changes(sh, last_shape(sh["family"]), saved)}


def _letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _account_index() -> set[str]:
    with tx() as conn:
        return {r["a"] for r in conn.execute("select upper(account_ref) as a from households where account_ref is not null")}


def run(data: bytes, *, source: str, replace: bool = True, min_fraction: float = 0.8, overrides: dict | None = None) -> dict:
    """Import the workbook. Returns {stats, changes, warnings}."""
    from .member_lookup import norm_address
    saved = legend()
    tabs = classify(parse(data), overrides)
    colors = with_shades(tabs, saved)
    rec = records(tabs, colors)
    sh = shape(tabs, colors)
    found = changes(sh, last_shape(sh["family"]), saved)
    _learn_colors(sh, colors)
    colors = legend()
    effects = {k: c.get("effect") for k, c in colors.items() if c.get("effect")}
    from . import org
    default_type = (org.get().get("member_types") or {}).get("default") or "full"
    warnings, stats = [], {}
    tag = f"workbook ({source})"

    kinds = {t["kind"] for t in tabs}
    has_households = bool(kinds & {"households", "former"})
    ids: dict[str, int] = {}
    allow_removals = False
    if has_households:
        allow_removals = _import_households(rec, effects, default_type, replace, min_fraction, tag, ids, stats, warnings)
    hid = lambda acct, addr: ids.get((acct or "").upper()) or ids.get(f"addr:{norm_address(addr)}")
    with tx() as conn:
        if has_households:
            _import_vehicles_and_cards(conn, rec, hid, allow_removals, tag, stats)
        if "vendors" in kinds:
            _import_vendor_cards(conn, rec, allow_removals and has_households, tag, stats)
        if "golf_carts" in kinds:
            _import_carts(conn, rec, hid, allow_removals and has_households, tag, stats, found)
        if rec["contacts"]:
            _import_contacts(conn, rec["contacts"], hid, stats, found)
        conn.execute("insert into workbook_snapshots (source, shape) values (%s, %s)", (source, Jsonb(sh)))
        conn.execute("delete from workbook_snapshots where at < now() - interval '2 years'")
    from . import people, vehicles
    people.clear()
    stats["vehicles_linked"] = vehicles.relink()
    return {"stats": stats, "changes": found, "warnings": warnings}


def _import_households(rec, effects, default_type, replace, min_fraction, tag, ids, stats, warnings) -> bool:
    """Households by account number, else by address. A household listed twice keeps the active row.
    Returns whether removals are allowed (the emptied-file guard)."""
    from .member_lookup import norm_address
    by_acct: dict = {}
    for h in rec["households"]:
        k = (h["account_ref"] or "").upper() or f"addr:{norm_address(h['address'])}"
        if k in by_acct and by_acct[k]["status"] == "active":
            continue
        effect = effects.get(h["row_color"]) or ""
        if effect == "inactive":
            h["status"] = "inactive"
        h["member_type"] = effect[5:] if effect.startswith("type:") else default_type
        by_acct[k] = h
    with tx() as conn:
        active_now = conn.execute("select count(*)::int as n from households where status = 'active'").fetchone()["n"]
    incoming_active = sum(1 for h in by_acct.values() if h["status"] == "active")
    allow_removals = replace and not (active_now and incoming_active < active_now * min_fraction)
    if replace and not allow_removals:
        warnings.append(f"The workbook has {incoming_active} active households, fewer than {int(min_fraction * 100)}% of the "
                        f"{active_now} Cypress has now, so nobody was marked inactive and nothing was removed. Check the file.")
    s = {"created": 0, "updated": 0, "deactivated": 0}
    with tx() as conn:
        for k, h in by_acct.items():
            row = None
            if h["account_ref"]:
                row = conn.execute("select id from households where upper(account_ref) = upper(%s)", (h["account_ref"],)).fetchone()
            if not row and h["address"]:
                cand = conn.execute("select id, address from households where account_ref is null").fetchall()
                row = next((c for c in cand if norm_address(c["address"]) == norm_address(h["address"])), None)
            vals = (h["name"], h["address"], h["account_ref"], h["status"], h["notes"], h["people"], h["row_color"],
                    Jsonb(h["highlights"]), tag, h["member_type"])
            if row:
                conn.execute("""update households set name = %s, address = coalesce(%s, address), account_ref = coalesce(%s, account_ref),
                                  status = %s, notes = %s, people = %s, row_color = %s, highlights = %s, source = %s,
                                  member_type = %s, updated_at = now()
                                where id = %s""", (*vals, row["id"]))
                ids[k] = row["id"]
                s["updated"] += 1
            else:
                ids[k] = conn.execute("""insert into households (name, address, account_ref, status, notes, people, row_color,
                                           highlights, source, member_type) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
                                      vals).fetchone()["id"]
                s["created"] += 1
        if allow_removals:
            s["deactivated"] = conn.execute(
                """update households set status = 'inactive', updated_at = now()
                   where status = 'active' and source like 'workbook%%' and not (id = any(%s))""", (list(ids.values()),)).rowcount
    stats["households"] = s
    return allow_removals


def _import_vehicles_and_cards(conn, rec, hid, allow_removals, tag, stats) -> None:
    if True:
        # Vehicles: by plate (the workbook has no plate state).
        keep, v = [], {"created": 0, "updated": 0, "removed": 0}
        for x in rec["vehicles"]:
            if not x["plate_norm"]:
                continue
            r = conn.execute(
                """insert into vehicles (household_id, plate, plate_norm, state, make, model, year, tag, notes, account_ref, address,
                                         highlights, source)
                   values (%s, %s, %s, '', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (plate_norm, state) do update set household_id = excluded.household_id, plate = excluded.plate,
                     make = excluded.make, model = excluded.model, year = excluded.year, tag = excluded.tag, notes = excluded.notes,
                     account_ref = excluded.account_ref, address = excluded.address, highlights = excluded.highlights,
                     source = excluded.source, updated_at = now()
                   returning id, (xmax = 0) as inserted""",
                (hid(x["account_ref"], x["address"]), x["plate"], x["plate_norm"], x["make"], x["model"], x["year"], x["tag"],
                 x["notes"], x["account_ref"], x["address"], Jsonb(x["highlights"]), tag)).fetchone()
            keep.append(r["id"])
            v["created" if r["inserted"] else "updated"] += 1
        if allow_removals:
            v["removed"] = conn.execute("delete from vehicles where source like 'workbook%%' and not (id = any(%s))", (keep,)).rowcount
        stats["vehicles"] = v

        keep_cards = []
        for g in rec["gate_cards"]:
            keep_cards.append(conn.execute(
                """insert into gate_cards (card, household_id, kind, color, source) values (%s, %s, 'member', %s, %s)
                   on conflict (card, kind) do update set household_id = excluded.household_id, color = excluded.color,
                     source = excluded.source, updated_at = now() returning id""",
                (g["card"], hid(g["account_ref"], g["address"]), g["color"], tag)).fetchone()["id"])
        removed = 0
        if allow_removals:
            removed = conn.execute("""delete from gate_cards where kind = 'member' and source like 'workbook%%'
                                      and not (id = any(%s))""", (keep_cards,)).rowcount
        stats["gate_cards"] = {"member": len(keep_cards), "removed": removed}


def _import_vendor_cards(conn, rec, allow_removals, tag, stats) -> None:
    keep_cards = []
    if True:
        for g in rec["vendor_cards"]:
            keep_cards.append(conn.execute(
                """insert into gate_cards (card, holder, kind, issued, notes, color, source) values (%s, %s, 'vendor', %s, %s, %s, %s)
                   on conflict (card, kind) do update set holder = excluded.holder, issued = excluded.issued, notes = excluded.notes,
                     color = excluded.color, source = excluded.source, updated_at = now() returning id""",
                (g["card"], g["holder"], g["issued"], g["notes"], g["color"], tag)).fetchone()["id"])
        removed = 0
        if allow_removals:
            removed = conn.execute("""delete from gate_cards where kind = 'vendor' and source like 'workbook%%'
                                      and not (id = any(%s))""", (keep_cards,)).rowcount
        stats["vendor_cards"] = {"imported": len(keep_cards), "removed": removed}


def _import_carts(conn, rec, hid, allow_removals, tag, stats, found) -> None:
    if True:
        keep_carts, unmatched = [], 0
        for g in rec["golf_carts"]:
            h = hid(g["account_ref"], g["address"])
            if not h and g["account_ref"]:
                r = conn.execute("select id from households where upper(account_ref) = upper(%s)", (g["account_ref"],)).fetchone()
                h = r["id"] if r else None
            unmatched += 0 if h else 1
            keep_carts.append(conn.execute(
                """insert into golf_carts (tag, household_id, account_ref, description, serial, waiver, status_note, color, source)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (tag) do update set household_id = excluded.household_id, account_ref = excluded.account_ref,
                     description = excluded.description, serial = excluded.serial, waiver = excluded.waiver,
                     status_note = excluded.status_note, color = excluded.color, source = excluded.source, updated_at = now()
                   returning id""",
                (g["tag"], h, g["account_ref"], g["description"], g["serial"], g["waiver"], g["status_note"], g["color"], tag)).fetchone()["id"])
        removed_carts = 0
        if allow_removals:
            removed_carts = conn.execute("delete from golf_carts where source like 'workbook%%' and not (id = any(%s))", (keep_carts,)).rowcount
        stats["golf_carts"] = {"imported": len(keep_carts), "unmatched": unmatched, "removed": removed_carts}
        if unmatched:
            found.append({"level": "info", "what": f"{unmatched} golf cart{'s' if unmatched != 1 else ''} didn't match a household account."})


def _import_contacts(conn, contacts, hid, stats, found) -> None:
    """Emails and phones onto existing households (by account, else address). Emails are added or moved, never
    removed here: a member may already sign in with one."""
    from .member_lookup import norm_address
    homes = conn.execute("select id, upper(account_ref) as a, address from households").fetchall()
    by_a = {h["a"]: h["id"] for h in homes if h["a"]}
    by_addr = {norm_address(h["address"]): h["id"] for h in homes if h["address"]}
    c = {"households": 0, "emails": 0, "phones": 0, "unmatched": 0, "shared": 0}
    taken: dict[str, int] = {}
    for x in contacts:
        h = hid(x["account_ref"], x["address"]) or by_a.get((x["account_ref"] or "").upper()) or by_addr.get(norm_address(x["address"]))
        if not h:
            c["unmatched"] += 1
            continue
        c["households"] += 1
        for i, e in enumerate(x["emails"]):
            if e["email"] in taken and taken[e["email"]] != h:
                c["shared"] += 1          # one email on several accounts (e.g. an owner of two lots): stays with the first
                continue
            taken[e["email"]] = h
            name = e["name"] or (x["people"][i] if i < len(x["people"]) else None)
            conn.execute("""insert into member_emails (household_id, email, name) values (%s, %s, %s)
                            on conflict (email) do update set household_id = excluded.household_id,
                              name = coalesce(excluded.name, member_emails.name)""", (h, e["email"], name))
            c["emails"] += 1
        if x["phones"]:
            conn.execute("update households set phones = %s, updated_at = now() where id = %s", (x["phones"], h))
            c["phones"] += len(x["phones"])
        if x.get("balance") is not None:     # stored only; nothing uses dues balances yet
            conn.execute("update households set dues_balance = %s, dues_balance_at = now() where id = %s", (x["balance"], h))
            c["balances"] = c.get("balances", 0) + 1
    stats["contacts"] = c
    if c["shared"]:
        found.append({"level": "info", "what": f"{c['shared']} email address{'es are' if c['shared'] != 1 else ' is'} on more than one "
                                               "account; each stays linked to the first account it appears on."})
    if c["unmatched"]:
        found.append({"level": "info", "what": f"{c['unmatched']} contact row{'s' if c['unmatched'] != 1 else ''} didn't match a household account or address."})


def _learn_colors(sh: dict, colors: dict) -> None:
    """Add colors seen for the first time to the legend (no meaning yet), and record where each appears."""
    from . import org
    changed = False
    saved = legend()
    for k, v in sh["colors"].items():
        c = saved.setdefault(k, {"name": color_name(k), "meaning": None, "effect": None, "new": True})
        for a in colors.get(k, {}).get("aliases") or []:
            if a not in (c.get("aliases") or []):
                c["aliases"] = [*(c.get("aliases") or []), a]
                changed = True
        where = sorted(v["where"], key=lambda w: -v["where"][w])[:4]
        if c.get("where") != where or c.get("count") != v["fills"] + v["text"]:
            c["where"], c["count"] = where, v["fills"] + v["text"]
            changed = True
    if changed or any(k not in legend() for k in saved):
        with tx() as conn:
            conn.execute("""insert into org_settings (key, value, updated_by) values ('member_colors', %s, 'workbook import')
                            on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by, updated_at = now()""",
                         (Jsonb({"colors": saved}),))
        org.clear()


def color_meaning(h: str | None) -> dict | None:
    if not h:
        return None
    c = legend().get(h) or {}
    return {"hex": h, "name": c.get("name") or color_name(h), "meaning": c.get("meaning")}
