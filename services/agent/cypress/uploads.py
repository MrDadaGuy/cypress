"""Photo and video uploads for issue reports.

Photos go through the API: decoded and re-encoded as JPEG (max 2048 px), which normalizes formats and
applies the camera rotation. Videos are too big for an API request, so the browser uploads them straight
to the private bucket with a one-time signed link, then the API confirms the object (type and size).
Staff view videos through short-lived signed links. Limits are admin settings (org_settings 'uploads').
"""

import io
import logging
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import settings
from .db import tx

log = logging.getLogger(__name__)
MAX_BYTES = 12 * 1024 * 1024
MAX_SIDE = 2048
MAX_PER_MESSAGE = 4
LOCAL_DIR = Path(__file__).resolve().parent.parent / ".uploads"
Image.MAX_IMAGE_PIXELS = 60_000_000  # refuse decompression bombs


class UploadError(Exception):
    pass


def _bucket():
    from google.cloud import storage
    return storage.Client(project=settings.project).bucket(settings.uploads_bucket)


def clean_jpeg(data: bytes) -> tuple[bytes, int, int]:
    if len(data) > MAX_BYTES:
        raise UploadError("That photo is too large (12 MB max).")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise UploadError("That file isn't a photo I can read. Try a JPEG or PNG.") from None
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)  # no exif= argument: metadata is dropped
    return out.getvalue(), img.width, img.height


def save(data: bytes, *, channel: str, owner: str) -> str:
    """Clean and store one photo. Returns its upload id."""
    jpeg, w, h = clean_jpeg(data)
    uid = uuid.uuid4()
    path = f"photos/{uid}.jpg"
    if settings.uploads_bucket:
        _bucket().blob(path).upload_from_string(jpeg, content_type="image/jpeg")
    else:
        (LOCAL_DIR / "photos").mkdir(parents=True, exist_ok=True)
        (LOCAL_DIR / path).write_bytes(jpeg)
    with tx() as conn:
        conn.execute("insert into uploads (id, channel, owner, path, bytes, width, height) values (%s, %s, %s, %s, %s, %s, %s)",
                     (uid, channel, owner[:200], path, len(jpeg), w, h))
    return str(uid)


# ---------------- videos ----------------

VIDEO_TYPES = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm", "video/3gpp": "3gp", "video/x-m4v": "m4v"}
UPLOAD_DEFAULTS = {"videos": True, "max_video_mb": 100}


def upload_settings() -> dict:
    from . import org
    return {**UPLOAD_DEFAULTS, **(org.get().get("uploads") or {})}


def _signed(path: str, method: str, content_type: str | None = None, minutes: int = 30) -> str:
    """V4 signed URL, signed as the API's own service account (no key file)."""
    from datetime import timedelta

    import google.auth
    import google.auth.transport.requests
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    kw = {"service_account_email": settings.run_sa or getattr(creds, "service_account_email", None),
          "access_token": creds.token} if settings.run_sa or hasattr(creds, "service_account_email") else {}
    return _bucket().blob(path).generate_signed_url(version="v4", expiration=timedelta(minutes=minutes), method=method,
                                                    content_type=content_type, **kw)


def start_video(content_type: str, size: int, *, channel: str, owner: str) -> dict:
    """Reserve a video upload. Returns {upload_id, url, method, headers} for the browser to PUT to."""
    s = upload_settings()
    if not s.get("videos"):
        raise UploadError("Videos aren't accepted right now. Try a photo instead.")
    ext = VIDEO_TYPES.get((content_type or "").split(";")[0].lower())
    if not ext:
        raise UploadError("That video format isn't supported. Try MP4 or MOV.")
    limit = int(s["max_video_mb"]) * 1024 * 1024
    if size <= 0 or size > limit:
        raise UploadError(f"That video is too large ({s['max_video_mb']} MB max). Try a shorter clip.")
    uid = uuid.uuid4()
    path = f"videos/{uid}.{ext}"
    ct = content_type.split(";")[0].lower()
    with tx() as conn:
        conn.execute("""insert into uploads (id, channel, owner, path, bytes, kind, content_type, status)
                        values (%s, %s, %s, %s, %s, 'video', %s, 'pending')""", (uid, channel, owner[:200], path, size, ct))
    if settings.uploads_bucket:
        url = _signed(path, "PUT", ct)
    else:
        url = f"/api/chat/upload/{uid}/local"                       # dev: the API stores it on disk
    return {"upload_id": str(uid), "url": url, "method": "PUT", "headers": {"content-type": ct}}


def finish_video(upload_id: str) -> str:
    """Confirm the browser's upload landed and fits the limits; then it can be attached."""
    if not _is_uuid(upload_id):
        raise UploadError("Unknown upload.")
    with tx() as conn:
        row = conn.execute("select path, content_type, status from uploads where id = %s and kind = 'video'", (upload_id,)).fetchone()
    if not row:
        raise UploadError("Unknown upload.")
    if row["status"] == "ready":
        return upload_id
    limit = int(upload_settings()["max_video_mb"]) * 1024 * 1024
    if settings.uploads_bucket:
        blob = _bucket().get_blob(row["path"])
        size = blob.size if blob else 0
    else:
        f = LOCAL_DIR / row["path"]
        size = f.stat().st_size if f.exists() else 0
    if not size:
        raise UploadError("The video didn't finish uploading. Please try again.")
    if size > limit:
        if settings.uploads_bucket:
            _bucket().blob(row["path"]).delete()
        raise UploadError("That video is too large.")
    with tx() as conn:
        conn.execute("update uploads set status = 'ready', bytes = %s where id = %s", (size, upload_id))
    return upload_id


def save_local_video(upload_id: str, data: bytes) -> None:
    """Dev only (no bucket): store the PUT body for a pending video."""
    with tx() as conn:
        row = conn.execute("select path from uploads where id = %s and kind = 'video' and status = 'pending'", (upload_id,)).fetchone()
    if not row:
        raise UploadError("Unknown upload.")
    (LOCAL_DIR / "videos").mkdir(parents=True, exist_ok=True)
    (LOCAL_DIR / row["path"]).write_bytes(data)


def view_url(upload_id: str) -> str | None:
    """Short-lived link for staff to play a video."""
    if not _is_uuid(upload_id):
        return None
    with tx() as conn:
        row = conn.execute("select path from uploads where id = %s and kind = 'video' and status = 'ready' and removed_at is null", (upload_id,)).fetchone()
    if not row:
        return None
    return _signed(row["path"], "GET", minutes=60) if settings.uploads_bucket else f"/api/admin/uploads/{upload_id}"


def media_for_ticket(ticket_id: int, *, removed: bool = False) -> list[dict]:
    """A ticket's attachments; with removed=True, the ones retention removed (kind and when)."""
    cond = "removed_at is not null" if removed else "removed_at is null"
    with tx() as conn:
        return [dict(r) for r in conn.execute(
            f"""select id::text, kind, removed_at from uploads where ticket_id = %s and status = 'ready' and {cond}
                order by created_at""", (ticket_id,))]


def usable(ids: list[str]) -> list[str]:
    """Upload ids that exist, are recent, and aren't attached to a ticket yet (unguessable UUIDs)."""
    ids = [i for i in ids[:MAX_PER_MESSAGE] if _is_uuid(i)]
    if not ids:
        return []
    with tx() as conn:
        rows = conn.execute("""select id::text from uploads where id = any(%s::uuid[]) and ticket_id is null
                               and status = 'ready' and created_at > now() - interval '2 hours'""", (ids,)).fetchall()
    found = {r["id"] for r in rows}
    return [i for i in ids if i in found]


def attach(ids: list[str], ticket_id: int) -> int:
    if not ids:
        return 0
    with tx() as conn:
        return conn.execute("update uploads set ticket_id = %s where id = any(%s::uuid[]) and ticket_id is null",
                            (ticket_id, ids)).rowcount


def for_ticket(ticket_id: int) -> list[str]:
    with tx() as conn:
        return [r["id"] for r in conn.execute(
            "select id::text from uploads where ticket_id = %s and kind = 'photo' and removed_at is null order by created_at", (ticket_id,))]


def read(upload_id: str) -> bytes | None:
    if not _is_uuid(upload_id):
        return None
    with tx() as conn:
        row = conn.execute("select path from uploads where id = %s and removed_at is null", (upload_id,)).fetchone()
    if not row:
        return None
    if settings.uploads_bucket:
        return _bucket().blob(row["path"]).download_as_bytes()
    f = LOCAL_DIR / row["path"]
    return f.read_bytes() if f.exists() else None


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except ValueError:
        return False
