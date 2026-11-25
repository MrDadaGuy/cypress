"""Photo uploads: metadata stripped, size/format checks, and linking to tickets."""

import io

from PIL import Image

from cypress import uploads


def _jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (3000, 1500), (40, 120, 80))
    exif = Image.Exif()
    exif[0x8825] = {2: (30.0, 19.0, 36.0), 4: (97.0, 52.0, 25.0)}   # GPSInfo
    exif[0x0110] = "Pixel 99"                                         # camera model
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_clean_strips_metadata_and_resizes():
    raw = _jpeg_with_gps()
    assert Image.open(io.BytesIO(raw)).getexif()                     # had metadata
    cleaned, w, h = uploads.clean_jpeg(raw)
    img = Image.open(io.BytesIO(cleaned))
    assert not img.getexif() and (w, h) == (2048, 1024)


def test_rejects_non_images():
    import pytest
    with pytest.raises(uploads.UploadError):
        uploads.clean_jpeg(b"%PDF-1.7 not a photo")


def test_upload_endpoint_and_attach(client):
    r = client.post("/api/chat/upload", files={"file": ("gate.jpg", _jpeg_with_gps(), "image/jpeg")})
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]
    assert uploads.usable([uid, "not-a-uuid"]) == [uid]
    assert uploads.attach([uid], 4242) == 1
    assert uploads.usable([uid]) == []                                # can't be reused once attached
    assert uploads.for_ticket(4242) == [uid]
    assert client.post("/api/chat/upload", files={"file": ("x.txt", b"hello", "text/plain")}).status_code == 422


def test_video_upload_flow(client):
    from cypress import uploads
    r = client.post("/api/chat/upload-video", json={"content_type": "video/quicktime", "bytes": 2048})
    assert r.status_code == 200
    slot = r.json()
    assert slot["url"].endswith("/local") and slot["headers"]["content-type"] == "video/quicktime"
    vid = slot["upload_id"]
    assert uploads.usable([vid]) == []                                          # not attachable until confirmed
    assert client.post(f"/api/chat/upload/{vid}/done").status_code == 422       # nothing uploaded yet
    assert client.put(slot["url"], content=b"\x00" * 2048, headers=slot["headers"]).status_code == 200
    assert client.post(f"/api/chat/upload/{vid}/done").json() == {"upload_id": vid}
    assert uploads.usable([vid]) == [vid]
    assert uploads.attach([vid], 987654) == 1
    assert [(m["id"], m["kind"]) for m in uploads.media_for_ticket(987654)] == [(vid, "video")]
    assert uploads.for_ticket(987654) == []                                     # photos only
    assert client.get(f"/api/admin/uploads/{vid}/link").json()["url"].endswith(vid)


def test_video_limits(client):
    assert client.post("/api/chat/upload-video", json={"content_type": "application/pdf", "bytes": 10}).status_code == 422
    r = client.post("/api/chat/upload-video", json={"content_type": "video/mp4", "bytes": 500 * 1024 * 1024})
    assert r.status_code == 422 and "too large" in r.json()["detail"]
    client.put("/api/admin/org/uploads", json={"videos": False, "max_video_mb": 100})
    from cypress import org
    org.clear()
    assert "aren't accepted" in client.post("/api/chat/upload-video", json={"content_type": "video/mp4", "bytes": 10}).json()["detail"]
    client.put("/api/admin/org/uploads", json={"videos": True, "max_video_mb": 100})
    org.clear()
