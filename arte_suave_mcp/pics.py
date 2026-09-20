"""Training pictures: an experiment in getting a user's photo out to an MCP tool.

MCP has no file transfer yet, so there are two routes in (spec: features.md F2):

* inline — the caller passes real base64 bytes in the tool call. Only clients
  that hold the actual file can do that; a chat model looking at an attached
  photo cannot, and whatever it invents is rejected here.
* upload link — the tool hands out a short-lived ``/upload/<token>`` URL. The
  page behind it posts the file straight to S3 with a presigned POST, so the
  bytes never pass through MCP (or this Lambda).

Pictures live in a private bucket under ``pics/<user_id>/<ts>-<id>``. Locally
(no bucket configured) inline uploads are kept in a process list so the tools
still work in dev and tests.
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import datetime as dt
import html
import json
import os
import re
import secrets
import time
import urllib.parse

MAX_INLINE_BYTES = 3 * 1024 * 1024  # base64 in a tool call is for small files only
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # a phone photo, comfortably
TICKET_TTL = 900  # seconds an upload link stays valid
VIEW_TTL = 3600  # seconds a view_url stays valid
MAX_NOTE = 500
LIST_LIMIT = 20
UPLOAD_PREFIX = "/upload/"

# Public origin of the current request, set by the server's guard so the tool
# can build an absolute upload link.
public_base: contextvars.ContextVar[str] = contextvars.ContextVar(
    "public_base", default="http://localhost:8080"
)

# Local/dev fallback when no bucket / table is configured.
_memory: list[dict] = []
_tickets: dict[str, dict] = {}
_s3_ref = None
_table_ref = None


class PicError(ValueError):
    """A picture was refused; the message is safe to show the assistant."""


def _now() -> int:
    return int(time.time())


def _bucket() -> str | None:
    return os.environ.get("ARTESUAVE_PICS_BUCKET")


def _s3():
    global _s3_ref
    if _s3_ref is None:
        import boto3
        from botocore.config import Config

        # Regional endpoint + SigV4: the global endpoint answers presigned POSTs
        # for a young eu-north-1 bucket with a redirect browsers won't follow.
        region = os.environ.get("AWS_REGION", "eu-north-1")
        _s3_ref = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3_ref


def _table():
    global _table_ref
    if _table_ref is None:
        import boto3

        _table_ref = boto3.resource("dynamodb").Table(
            os.environ["ARTESUAVE_SESSION_TABLE"]
        )
    return _table_ref


# -- format -------------------------------------------------------------------
def sniff(data: bytes) -> str | None:
    """Image MIME type from magic bytes, or None if it isn't a known image."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"heif"):
        return "image/heic"
    return None


def _new_key(user_id: str) -> str:
    # <ts> first so keys sort chronologically within a user's prefix
    return f"pics/{user_id}/{_now()}-{secrets.token_hex(4)}"


def _clean_note(note: str | None) -> str | None:
    return (note or "").strip()[:MAX_NOTE] or None


def _present(key: str, size: int, mime: str | None, note: str | None) -> dict:
    ts = int(key.rsplit("/", 1)[1].split("-", 1)[0])
    out = {
        "pic_id": key.rsplit("/", 1)[1],
        "uploaded_at": dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "size": size,
    }
    if mime:
        out["type"] = mime
    if note:
        out["note"] = note
    return out


# -- inline route -------------------------------------------------------------
_USE_LINK = "call upload_training_pic without image_base64 and give the user the upload_url"


def store_inline(user_id: str, image_base64: str, note: str | None = None) -> dict:
    """Decode, verify and store a picture passed as base64 in the tool call."""
    raw = re.sub(r"^data:[^,]*,", "", image_base64.strip())
    raw = re.sub(r"\s+", "", raw)
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise PicError(
            "image_base64 is not valid base64 — it looks invented or truncated. "
            f"If you can't read the file's real bytes, {_USE_LINK}."
        ) from None
    mime = sniff(data)
    if mime is None:
        raise PicError(
            "the decoded bytes are not a JPEG, PNG, WebP, GIF or HEIC image. "
            f"If you can't read the file's real bytes, {_USE_LINK}."
        )
    if len(data) > MAX_INLINE_BYTES:
        raise PicError(
            f"picture is {len(data)} bytes; inline uploads are capped at "
            f"{MAX_INLINE_BYTES}. For larger files, {_USE_LINK}."
        )
    key = _new_key(user_id)
    note = _clean_note(note)
    if _bucket():
        extra = {"Metadata": {"note": urllib.parse.quote(note)}} if note else {}
        _s3().put_object(Bucket=_bucket(), Key=key, Body=data, ContentType=mime, **extra)
    else:
        _memory.append({"key": key, "data": data, "type": mime, "note": note})
    return _present(key, len(data), mime, note)


# -- listing ------------------------------------------------------------------
def list_pics(user_id: str) -> list[dict]:
    """The user's pictures, newest first."""
    prefix = f"pics/{user_id}/"
    if not _bucket():
        rows = [m for m in _memory if m["key"].startswith(prefix)]
        return [
            _present(m["key"], len(m["data"]), m["type"], m["note"])
            for m in sorted(rows, key=lambda m: m["key"], reverse=True)[:LIST_LIMIT]
        ]
    s3, bucket = _s3(), _bucket()
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys += [(o["Key"], o["Size"]) for o in page.get("Contents", [])]
    out = []
    for key, size in sorted(keys, reverse=True)[:LIST_LIMIT]:
        head = s3.head_object(Bucket=bucket, Key=key)
        note = urllib.parse.unquote(head.get("Metadata", {}).get("note", "")) or None
        row = _present(key, size, head.get("ContentType"), note)
        row["view_url"] = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=VIEW_TTL
        )
        out.append(row)
    return out


# -- upload-link route --------------------------------------------------------
def create_ticket(user_id: str, note: str | None) -> str:
    """Mint a single-destination upload ticket; the token is the only credential."""
    token = secrets.token_urlsafe(32)
    item = {
        "pk": f"upload#{token}",
        "kind": "upload_ticket",
        "user_id": user_id,
        "key": _new_key(user_id),
        "ttl": _now() + TICKET_TTL,
    }
    note = _clean_note(note)
    if note:
        item["note"] = note
    if os.environ.get("ARTESUAVE_SESSION_TABLE"):
        _table().put_item(Item=item)
    else:
        _tickets[token] = item
    return token


def get_ticket(token: str) -> dict | None:
    if os.environ.get("ARTESUAVE_SESSION_TABLE"):
        item = _table().get_item(Key={"pk": f"upload#{token}"}).get("Item")
    else:
        item = _tickets.get(token)
    # DynamoDB's TTL sweep lags, so check expiry ourselves
    if not item or int(item["ttl"]) < _now():
        return None
    return item


def upload_url(token: str) -> str:
    return f"{public_base.get()}{UPLOAD_PREFIX}{token}"


def presigned_post(ticket: dict, redirect: str) -> dict:
    """Presigned S3 POST bound to the ticket's key: images only, size-capped."""
    fields = {"success_action_redirect": redirect}
    if ticket.get("note"):
        fields["x-amz-meta-note"] = urllib.parse.quote(ticket["note"])
    conditions = [{k: v} for k, v in fields.items()] + [
        ["content-length-range", 1, MAX_UPLOAD_BYTES],
        ["starts-with", "$Content-Type", "image/"],
    ]
    expires = max(int(ticket["ttl"]) - _now(), 1)
    return _s3().generate_presigned_post(
        _bucket(), ticket["key"], Fields=fields, Conditions=conditions, ExpiresIn=expires
    )


def _uploaded(ticket: dict) -> bool:
    from botocore.exceptions import ClientError

    try:
        _s3().head_object(Bucket=_bucket(), Key=ticket["key"])
    except ClientError:
        return False
    return True


def is_upload_path(path: str) -> bool:
    return path.startswith(UPLOAD_PREFIX) and len(path) > len(UPLOAD_PREFIX)


async def _respond(send, status: int, body: str, content_type: str = "text/html; charset=utf-8"):
    headers = [
        (b"content-type", content_type.encode()),
        (b"cache-control", b"no-store"),
        (b"referrer-policy", b"no-referrer"),  # the URL carries the ticket
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body.encode()})


async def handle(scope, receive, send) -> None:
    """Serve ``/upload/<token>``: the upload form, its JSON twin, or the receipt."""
    token = scope.get("path", "")[len(UPLOAD_PREFIX):]
    query = urllib.parse.parse_qs(scope.get("query_string", b"").decode())
    ticket = get_ticket(token) if scope.get("method") == "GET" else None
    if ticket is None:
        await _respond(send, 404, _message_page(
            "Linket er udløbet", "Bed Claude om et nyt upload-link."))
        return
    if not _bucket():
        await _respond(send, 503, _message_page(
            "Upload er ikke sat op", "Serveren har ingen billed-lagring konfigureret."))
        return
    if _uploaded(ticket):
        await _respond(send, 200, _message_page(
            "Billedet er uploadet ✓", "Du kan gå tilbage til samtalen."))
        return
    post = presigned_post(ticket, upload_url(token))
    if query.get("format") == ["json"]:
        payload = {
            **post,
            "how": "multipart/form-data POST to url: all fields, plus Content-Type "
            "(the image's MIME type), then the file as the last field, named 'file'",
        }
        await _respond(send, 200, json.dumps(payload), "application/json")
        return
    await _respond(send, 200, upload_page(post))


# -- pages --------------------------------------------------------------------
_STYLE = """
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: #0f1115; color: #f2f3f5; padding: 24px;
  }
  .card {
    width: 100%; max-width: 380px; background: #181b21; border: 1px solid #262a33;
    border-radius: 16px; padding: 32px; box-shadow: 0 12px 40px rgba(0,0,0,.4);
  }
  h1 { font-size: 1.25rem; margin: 0 0 4px; }
  p.sub { margin: 0 0 24px; color: #9aa1ad; font-size: .9rem; }
  input[type=file] { width: 100%; font-size: 1rem; color: #c3c9d4; }
  button {
    width: 100%; margin-top: 24px; padding: 12px; border: 0; border-radius: 10px;
    background: #d33a3a; color: #fff; font-size: 1rem; font-weight: 600; cursor: pointer;
  }
  button:hover { background: #e04545; }
  button:disabled { background: #4a2326; color: #b99; cursor: default; }
"""


def _page(body: str) -> str:
    return f"""<!doctype html>
<html lang="da">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Træningsbillede — Arte Suave</title>
<style>{_STYLE}</style>
</head>
<body>
{body}
</body>
</html>"""


def _message_page(title: str, sub: str) -> str:
    return _page(
        f'  <div class="card"><h1>{html.escape(title)}</h1>'
        f'<p class="sub">{html.escape(sub)}</p></div>'
    )


def upload_page(post: dict) -> str:
    """One-field form that posts the chosen photo straight to S3."""
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in post["fields"].items()
    )
    # S3 requires every policy field before the file, and Content-Type must match
    # the policy's image/ prefix — it's filled in from the chosen file.
    return _page(f"""  <form class="card" method="post" enctype="multipart/form-data"
        action="{html.escape(post["url"])}">
    <h1>Træningsbillede</h1>
    <p class="sub">Vælg et billede — det gemmes privat på din Arte Suave-forbindelse.</p>
    {hidden}
    <input type="hidden" name="Content-Type" id="ct" value="image/jpeg">
    <input type="file" name="file" id="file" accept="image/*" required>
    <button type="submit" id="go">Upload</button>
  </form>
  <script>
    const file = document.getElementById("file");
    file.addEventListener("change", () => {{
      if (file.files[0] && file.files[0].type) {{
        document.getElementById("ct").value = file.files[0].type;
      }}
    }});
    document.querySelector("form").addEventListener("submit", () => {{
      const go = document.getElementById("go");
      go.disabled = true; go.textContent = "Uploader…";
    }});
  </script>""")
