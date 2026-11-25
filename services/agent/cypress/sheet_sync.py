"""Nightly sync of CAM's membership workbook from Google Drive (its source of truth alongside Vantaca).

The file is shared with Cypress's service account as a Viewer. Cypress downloads it through the Drive API: a
Google Sheet is exported to .xlsx (hand-applied cell colors come through as fills), an .xlsx stored in Drive is
downloaded as is. Then workbook.run imports it exactly like a console upload, compares it with the last import
and the color legend, and records what changed. The file's link is an admin setting, so next year's file is a
paste and a "Check".

Note: if the owner turns on "Viewers can't download, print or copy" for the file, Viewers (including the service
account) can't download it; the sync then fails with that explanation.
"""

import logging
import re

from psycopg.types.json import Jsonb

from .db import tx

log = logging.getLogger(__name__)
DEFAULTS = {"enabled": False, "file_id": None, "file_url": None, "file_name": None,
            "contacts_file_id": None, "contacts_file_url": None, "contacts_file_name": None, "replace": True,
            "min_fraction": 0.8, "tab_kinds": {}}
DRIVE = "https://www.googleapis.com/drive/v3/files"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET = "application/vnd.google-apps.spreadsheet"
MAX_BYTES = 20_000_000


class SheetError(Exception):
    pass


def settings() -> dict:
    from . import org
    return {**DEFAULTS, **(org.get().get("member_sync") or {})}


def file_id(url_or_id: str) -> str:
    """A Drive file id from a Sheets link, a Drive file link, or the bare id."""
    s = (url_or_id or "").strip()
    m = (re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", s) or re.search(r"/file/d/([A-Za-z0-9_-]{20,})", s)
         or re.search(r"[?&]id=([A-Za-z0-9_-]{20,})", s))
    fid = m.group(1) if m else s
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", fid):
        raise SheetError("That doesn't look like a Google Sheets or Google Drive file link.")
    return fid


def service_account() -> str:
    from .config import settings as cfg
    return cfg.run_sa or "cypress-run@lhca-cypress.iam.gserviceaccount.com"


def _session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return AuthorizedSession(creds)


def _check(r, what: str) -> None:
    if r.status_code in (403, 404):
        detail = r.text[:300]
        if "cannotDownload" in detail or "cannotExport" in detail or "download" in detail.lower():
            raise SheetError("Cypress can see the file but isn't allowed to download it. In the file's share settings, "
                             "allow viewers to download, print and copy.")
        raise SheetError(f"Cypress can't open that file. Share it with {service_account()} as a Viewer.")
    if r.status_code >= 300:
        raise SheetError(f"Google Drive said {r.status_code} while {what}: {r.text[:200]}")


def fetch(fid: str) -> tuple[bytes, dict]:
    """(xlsx bytes, file metadata)."""
    s = _session()
    r = s.get(f"{DRIVE}/{fid}", params={"fields": "id,name,mimeType,modifiedTime,size", "supportsAllDrives": "true"}, timeout=30)
    _check(r, "reading the file's details")
    meta = r.json()
    if meta.get("mimeType") == SHEET:
        r = s.get(f"{DRIVE}/{fid}/export", params={"mimeType": XLSX}, timeout=120)
    elif meta.get("mimeType") == XLSX or (meta.get("name") or "").lower().endswith(".xlsx"):
        r = s.get(f"{DRIVE}/{fid}", params={"alt": "media", "supportsAllDrives": "true"}, timeout=120)
    else:
        raise SheetError(f"That file is a {meta.get('mimeType')}, not a Google Sheet or an .xlsx workbook.")
    _check(r, "downloading the file")
    if len(r.content) > MAX_BYTES:
        raise SheetError("That file is over 20 MB.")
    return r.content, meta


def check(url: str) -> dict:
    """Open the file and preview an import. Nothing is saved."""
    from . import workbook
    fid = file_id(url)
    data, meta = fetch(fid)
    return {"file_id": fid, "file_name": meta.get("name"), "modified": meta.get("modifiedTime"),
            **workbook.preview(data, settings()["tab_kinds"])}


def run(trigger: str = "nightly") -> dict:
    """Sync now. Records and returns the run."""
    from . import workbook
    s = settings()
    if not s["enabled"] or not s["file_id"]:
        return {"status": "skipped"}
    stats, status, error = {"changes": [], "warnings": []}, "ok", None
    try:
        # The membership workbook first, then the contacts file (emails attach to the households just imported).
        for fid in [x for x in (s["file_id"], s.get("contacts_file_id")) if x]:
            data, meta = fetch(fid)
            r = workbook.run(data, source="sync", replace=bool(s["replace"]), min_fraction=float(s["min_fraction"]),
                             overrides=s["tab_kinds"])
            stats.update({k: v for k, v in r["stats"].items()})
            stats["changes"] += [{**c, "what": f"{meta.get('name')}: {c['what']}"} for c in r["changes"]]
            stats["warnings"] += r["warnings"]
            stats.setdefault("files", []).append({"name": meta.get("name"), "modified": meta.get("modifiedTime")})
        if stats["warnings"] or any(c["level"] == "alert" for c in stats["changes"]):
            status = "warning"
    except SheetError as e:
        status, error = "failed", str(e)
    except Exception as e:
        log.exception("member sync failed")
        status, error = "failed", f"{type(e).__name__}: {e}"[:300]
    return _record(trigger, status, stats, error)


def _record(trigger: str, status: str, stats: dict, error: str | None) -> dict:
    with tx() as conn:
        row = conn.execute("""insert into member_sync_runs (trigger, status, stats, error) values (%s, %s, %s, %s)
                              returning id, at, trigger, status, stats, error""",
                           (trigger, status, Jsonb(stats), error)).fetchone()
        conn.execute("delete from member_sync_runs where at < now() - interval '1 year'")
    log.info("member sync %s", status, extra={"json_fields": {"member_sync": {
        "status": status, "households": (stats.get("households") or {}), "alerts": sum(
            1 for c in stats.get("changes") or [] if c.get("level") == "alert")}}})
    return row


def record_upload(stats: dict, changes: list, warnings: list) -> dict:
    """A console upload counts as a run too, so the history and 'what changed' cover both."""
    status = "warning" if warnings or any(c["level"] == "alert" for c in changes) else "ok"
    return _record("upload", status, {**stats, "changes": changes, "warnings": warnings}, None)


def nightly() -> dict:
    return run("nightly")


def recent(limit: int = 10) -> list[dict]:
    with tx() as conn:
        return conn.execute("select id, at, trigger, status, stats, error from member_sync_runs order by at desc limit %s",
                            (limit,)).fetchall()
