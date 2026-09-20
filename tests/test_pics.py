import base64

import pytest

from arte_suave_mcp import pics, service

# Smallest valid-looking payloads: real magic bytes + padding.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    monkeypatch.delenv("ARTESUAVE_PICS_BUCKET", raising=False)
    monkeypatch.delenv("ARTESUAVE_SESSION_TABLE", raising=False)
    pics._memory.clear()
    pics._tickets.clear()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---- format sniffing --------------------------------------------------------
def test_sniff_known_formats():
    assert pics.sniff(PNG) == "image/png"
    assert pics.sniff(JPEG) == "image/jpeg"
    assert pics.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert pics.sniff(b"GIF89a" + b"\x00" * 10) == "image/gif"
    assert pics.sniff(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 10) == "image/heic"
    assert pics.sniff(b"just some text") is None


# ---- inline base64 route ----------------------------------------------------
def test_inline_upload_stores_picture():
    res = service.upload_training_pic(image_base64=_b64(PNG), note="sparring, runde 3")
    assert res["status"] == "ok"
    assert res["data"]["type"] == "image/png"
    assert res["data"]["size"] == len(PNG)
    listed = service.get_training_pics()
    assert listed["status"] == "ok"
    assert len(listed["data"]) == 1
    assert listed["data"][0]["note"] == "sparring, runde 3"
    assert listed["data"][0]["type"] == "image/png"


def test_inline_upload_accepts_data_uri_and_whitespace():
    payload = "data:image/jpeg;base64," + _b64(JPEG)[:20] + "\n" + _b64(JPEG)[20:]
    res = service.upload_training_pic(image_base64=payload)
    assert res["status"] == "ok"
    assert res["data"]["type"] == "image/jpeg"


def test_invented_base64_is_rejected_with_guidance():
    res = service.upload_training_pic(image_base64="iVBORw0KGgo...truncated")
    assert res["status"] == "error"
    assert "upload_url" in res["message"]  # tells the assistant what to do instead
    assert service.get_training_pics()["data"] == []


def test_valid_base64_that_is_not_an_image_is_rejected():
    res = service.upload_training_pic(image_base64=_b64(b"hello, not an image at all"))
    assert res["status"] == "error"
    assert "image" in res["message"]


def test_inline_upload_size_cap(monkeypatch):
    monkeypatch.setattr(pics, "MAX_INLINE_BYTES", 32)
    res = service.upload_training_pic(image_base64=_b64(PNG))
    assert res["status"] == "error"
    assert "upload_url" in res["message"]


def test_pictures_are_per_user():
    service.upload_training_pic(image_base64=_b64(PNG))
    token = service.set_current_user("someone-else")
    try:
        assert service.get_training_pics()["data"] == []
    finally:
        service.reset_current_user(token)


# ---- upload-link route ------------------------------------------------------
def test_without_bytes_returns_upload_link():
    res = service.upload_training_pic(note="ny teknik")
    assert res["status"] == "ok"
    url = res["data"]["upload_url"]
    assert "/upload/" in url
    token = url.rsplit("/", 1)[1]
    ticket = pics.get_ticket(token)
    assert ticket["user_id"] == service._current_user.get()
    assert ticket["note"] == "ny teknik"
    assert ticket["key"].startswith(f"pics/{ticket['user_id']}/")


def test_expired_or_unknown_ticket_is_none(monkeypatch):
    token = pics.create_ticket("u1", None)
    assert pics.get_ticket("nope") is None
    monkeypatch.setattr(pics, "_now", lambda: 10**12)
    assert pics.get_ticket(token) is None


def test_upload_path_matching():
    assert pics.is_upload_path("/upload/abc")
    assert not pics.is_upload_path("/mcp")
    assert not pics.is_upload_path("/uploads")


def test_upload_page_posts_escaped_fields_to_target():
    html = pics.upload_page(
        {"url": "https://bucket.example/", "fields": {"key": "pics/u1/1-x", "policy": "p<>"}}
    )
    assert 'action="https://bucket.example/"' in html
    assert 'name="key" value="pics/u1/1-x"' in html
    assert "p&lt;&gt;" in html  # field values are escaped
    assert 'type="file"' in html and 'accept="image/*"' in html
