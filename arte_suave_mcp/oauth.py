"""Minimal OAuth 2.1 authorization server for the MCP endpoint.

This is what turns "paste a token" into "log in with your Arte Suave account".
claude.ai (and any MCP client that speaks the MCP Authorization spec) discovers
these endpoints, registers itself, sends the user to :func:`/authorize` — which
serves a login page — and exchanges the resulting code at :func:`/token` for an
access token it then presents as ``Authorization: Bearer``.

We are the authorization server because the gym portal is *not* one: it's a plain
PHP session-cookie login, so we can't delegate. Instead the login page collects
the user's Arte Suave email/password, verifies them against the portal, stores
them as SSM SecureStrings (encrypted under a dedicated KMS key that only the
Lambda role can decrypt), and mints our own token. Because portal cookies are
short-lived, storing the credentials is what lets the server silently re-login
later — a reused cookie alone would decay.

State lives in the same DynamoDB table as sessions, under ``oauth:*`` keys:
  oauth:client:<id>      registered client + its allowed redirect_uris (no TTL)
  oauth:code:<code>      short-lived auth code (PKCE), TTL ~5 min
  oauth:token:<sha256>   access token -> user_id, TTL ~30 days
  oauth:refresh:<sha256> refresh token -> user_id, TTL ~180 days

Tokens are stored hashed, never in the clear. Nothing here ever logs a secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse

from . import config, creds

# --- token shapes ------------------------------------------------------------
ACCESS_PREFIX = "asm_at_"
REFRESH_PREFIX = "asm_rt_"
CODE_PREFIX = "asm_ac_"
CLIENT_PREFIX = "asm_cl_"

CODE_TTL = 300  # 5 min
ACCESS_TTL = 30 * 24 * 3600  # 30 days
REFRESH_TTL = 180 * 24 * 3600  # 180 days

# OAuth request params carried through the login page (GET -> hidden fields -> POST).
_FORWARD_PARAMS = (
    "client_id", "redirect_uri", "state", "code_challenge",
    "code_challenge_method", "scope", "resource",
)

# small in-process cache so a warm connector doesn't hit DynamoDB every request
_token_cache: dict[str, tuple[str | None, float]] = {}
_TOKEN_CACHE_TTL = 60.0


def enabled() -> bool:
    """OAuth is live only when explicitly turned on and a table is available."""
    return bool(os.environ.get("ARTESUAVE_OAUTH")) and bool(
        os.environ.get("ARTESUAVE_SESSION_TABLE")
    )


# --- storage helpers ---------------------------------------------------------
_table_ref = None


def _table():
    global _table_ref
    if _table_ref is None:
        import boto3

        _table_ref = boto3.resource("dynamodb").Table(
            os.environ["ARTESUAVE_SESSION_TABLE"]
        )
    return _table_ref


def _get(pk: str) -> dict | None:
    return _table().get_item(Key={"pk": pk}).get("Item")


def _put(item: dict) -> None:
    _table().put_item(Item=item)


def _delete(pk: str) -> None:
    _table().delete_item(Key={"pk": pk})


def _now() -> int:
    return int(time.time())


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# --- client registration -----------------------------------------------------
def register_client(redirect_uris: list[str], name: str) -> dict:
    client_id = CLIENT_PREFIX + secrets.token_hex(16)
    _put(
        {
            "pk": f"oauth:client:{client_id}",
            "kind": "oauth-client",
            "redirect_uris": redirect_uris,
            "client_name": name or "",
            "created_at": _now(),
        }
    )
    return {
        "client_id": client_id,
        "client_id_issued_at": _now(),
        "redirect_uris": redirect_uris,
        "client_name": name or "",
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


def get_client(client_id: str) -> dict | None:
    if not client_id:
        return None
    return _get(f"oauth:client:{client_id}")


# --- authorization codes ------------------------------------------------------
def create_code(
    client_id: str, redirect_uri: str, code_challenge: str, user_id: str
) -> str:
    code = CODE_PREFIX + secrets.token_urlsafe(24)
    _put(
        {
            "pk": f"oauth:code:{code}",
            "kind": "oauth-code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "user_id": user_id,
            "ttl": _now() + CODE_TTL,
        }
    )
    return code


def consume_code(code: str) -> dict | None:
    if not code:
        return None
    item = _get(f"oauth:code:{code}")
    if not item:
        return None
    _delete(f"oauth:code:{code}")  # single use
    if int(item.get("ttl", 0)) < _now():
        return None
    return item


# --- tokens -------------------------------------------------------------------
def issue_tokens(user_id: str) -> tuple[str, str, int]:
    access = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    _put(
        {
            "pk": f"oauth:token:{_sha(access)}",
            "kind": "oauth-token",
            "user_id": user_id,
            "ttl": _now() + ACCESS_TTL,
        }
    )
    _put(
        {
            "pk": f"oauth:refresh:{_sha(refresh)}",
            "kind": "oauth-refresh",
            "user_id": user_id,
            "ttl": _now() + REFRESH_TTL,
        }
    )
    return access, refresh, ACCESS_TTL


def refresh_access(refresh_token: str) -> tuple[str, int] | None:
    if not refresh_token or not refresh_token.startswith(REFRESH_PREFIX):
        return None
    item = _get(f"oauth:refresh:{_sha(refresh_token)}")
    if not item or int(item.get("ttl", 0)) < _now():
        return None
    user_id = item["user_id"]
    access = ACCESS_PREFIX + secrets.token_urlsafe(32)
    _put(
        {
            "pk": f"oauth:token:{_sha(access)}",
            "kind": "oauth-token",
            "user_id": user_id,
            "ttl": _now() + ACCESS_TTL,
        }
    )
    return access, ACCESS_TTL


def resolve_access_token(token: str | None) -> str | None:
    """Map an OAuth access token to a user id, or None. Short-lived cached."""
    if not token or not token.startswith(ACCESS_PREFIX):
        return None
    key = _sha(token)
    now = time.monotonic()
    hit = _token_cache.get(key)
    if hit and hit[1] > now:
        return hit[0]
    item = _get(f"oauth:token:{key}")
    user_id = None
    if item and int(item.get("ttl", 0)) >= _now():
        user_id = item["user_id"]
    _token_cache[key] = (user_id, now + _TOKEN_CACHE_TTL)
    return user_id


# --- credentials --------------------------------------------------------------
def user_id_for(email: str) -> str:
    """Deterministic, non-reversible user id from an email. Same email -> same id
    (so re-login overwrites creds and reuses the session), and the email never
    appears in an SSM parameter name."""
    return "u" + _sha(email.strip().lower())[:16]


def verify_credentials(email: str, password: str) -> tuple[bool, str | None]:
    """Try a real portal login with the given creds. Returns (ok, error_msg)."""
    from .client import AuthError, PortalClient
    from .session_store import MemoryBackend, SessionStore

    client = PortalClient(lambda: (email, password), SessionStore(MemoryBackend()))
    try:
        client.get_authed(f"{config.ACCOUNT}{config.Q_BOOKINGS}")
        return True, None
    except AuthError:
        return False, "Forkert email eller adgangskode."
    except Exception:
        return False, "Kunne ikke kontakte Arte Suave lige nu. Prøv igen om lidt."
    finally:
        client.close()


def store_user_credentials(email: str, password: str) -> str:
    """Persist creds as SSM SecureStrings under the dedicated KMS key. Returns
    the user id. Never logs the values."""
    import boto3

    ssm = boto3.client("ssm")
    uid = user_id_for(email)
    key_id = os.environ.get("ARTESUAVE_CREDS_KMS_KEY_ID")
    for field, value in (("login", email), ("password", password)):
        kwargs = dict(
            Name=f"{creds.USERS_PREFIX}/{uid}/{field}",
            Value=value,
            Type="SecureString",
            Overwrite=True,
        )
        if key_id:
            kwargs["KeyId"] = key_id
        ssm.put_parameter(**kwargs)
    # a returning user may have changed their password; drop any cached value
    try:
        creds._ssm_param.cache_clear()
    except Exception:
        pass
    return uid


# --- PKCE ---------------------------------------------------------------------
def verify_pkce(verifier: str, challenge: str) -> bool:
    if not verifier or not challenge:
        return False
    calc = _b64url(hashlib.sha256(verifier.encode()).digest())
    return hmac.compare_digest(calc, challenge)


# --- ASGI plumbing ------------------------------------------------------------
_WELL_KNOWN_AS = "/.well-known/oauth-authorization-server"
_WELL_KNOWN_PR = "/.well-known/oauth-protected-resource"
_WELL_KNOWN_OIDC = "/.well-known/openid-configuration"


def is_oauth_path(path: str) -> bool:
    return (
        path.startswith(_WELL_KNOWN_AS)
        or path.startswith(_WELL_KNOWN_PR)
        or path == _WELL_KNOWN_OIDC
        or path in ("/register", "/authorize", "/token")
    )


def protected_resource_metadata_url(base_url: str) -> str:
    # RFC 9728: the resource is <base>/mcp -> metadata at .../oauth-protected-resource/mcp
    return f"{base_url}{_WELL_KNOWN_PR}/mcp"


async def _read_body(receive) -> bytes:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    return body


_CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type, authorization, mcp-protocol-version",
}


async def _send(send, status: int, body: bytes, content_type: str, extra: dict | None = None):
    headers = [(b"content-type", content_type.encode())]
    merged = dict(_CORS)
    if extra:
        merged.update(extra)
    for k, v in merged.items():
        headers.append((k.encode(), v.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _json(send, status: int, payload: dict, extra: dict | None = None):
    await _send(send, status, json.dumps(payload).encode(), "application/json", extra)


async def _redirect(send, location: str):
    await _send(send, 302, b"", "text/plain; charset=utf-8", {"location": location})


def _as_metadata(base_url: str) -> dict:
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "registration_endpoint": f"{base_url}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    }


def _pr_metadata(base_url: str) -> dict:
    return {
        "resource": f"{base_url}/mcp",
        "authorization_servers": [base_url],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    }


def _err_redirect(redirect_uri: str, state: str, error: str, desc: str) -> str:
    q = {"error": error, "error_description": desc}
    if state:
        q["state"] = state
    return f"{redirect_uri}?{urllib.parse.urlencode(q)}"


async def handle(scope, receive, send, base_url: str) -> None:
    """Serve one OAuth route. Assumes is_oauth_path(path) already matched."""
    method = scope.get("method", "GET").upper()
    path = scope.get("path", "")
    query = urllib.parse.parse_qs(scope.get("query_string", b"").decode())

    if method == "OPTIONS":
        await _send(send, 204, b"", "text/plain", None)
        return

    if path.startswith(_WELL_KNOWN_AS) or path == _WELL_KNOWN_OIDC:
        await _json(send, 200, _as_metadata(base_url))
        return
    if path.startswith(_WELL_KNOWN_PR):
        await _json(send, 200, _pr_metadata(base_url))
        return

    if path == "/register" and method == "POST":
        try:
            body = json.loads(await _read_body(receive) or b"{}")
        except ValueError:
            await _json(send, 400, {"error": "invalid_client_metadata"})
            return
        redirect_uris = body.get("redirect_uris") or []
        if not isinstance(redirect_uris, list) or not redirect_uris:
            await _json(
                send,
                400,
                {"error": "invalid_redirect_uri", "error_description": "redirect_uris required"},
            )
            return
        reg = register_client(redirect_uris, body.get("client_name", ""))
        await _json(send, 201, reg)
        return

    if path == "/authorize":
        await _authorize(scope, receive, send, method, query)
        return

    if path == "/token" and method == "POST":
        await _token(receive, send)
        return

    await _json(send, 404, {"error": "not_found"})


async def _authorize(scope, receive, send, method: str, query: dict) -> None:
    if method == "POST":
        form = urllib.parse.parse_qs((await _read_body(receive)).decode())
        params = {k: form.get(k, [""])[0] for k in (*_FORWARD_PARAMS, "email", "password")}
    else:
        params = {k: query.get(k, [""])[0] for k in _FORWARD_PARAMS}

    client_id = params["client_id"]
    redirect_uri = params["redirect_uri"]
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")

    client = get_client(client_id)
    if not client:
        await _send(send, 400, b"Unknown client.", "text/plain; charset=utf-8")
        return
    if redirect_uri not in (client.get("redirect_uris") or []):
        await _send(send, 400, b"Invalid redirect_uri.", "text/plain; charset=utf-8")
        return
    # From here, errors go back to the client via redirect (per OAuth spec).
    if params.get("code_challenge_method", "S256") != "S256" or not code_challenge:
        await _redirect(
            send,
            _err_redirect(redirect_uri, state, "invalid_request", "PKCE S256 required"),
        )
        return

    if method == "GET":
        await _send(send, 200, _login_page(params).encode(), "text/html; charset=utf-8")
        return

    # POST: verify credentials against the portal
    email = params.get("email", "").strip()
    password = params.get("password", "")
    if not email or not password:
        await _send(
            send, 200,
            _login_page(params, error="Udfyld email og adgangskode.").encode(),
            "text/html; charset=utf-8",
        )
        return
    ok, err = verify_credentials(email, password)
    if not ok:
        await _send(
            send, 200, _login_page(params, error=err).encode(),
            "text/html; charset=utf-8",
        )
        return

    user_id = store_user_credentials(email, password)
    code = create_code(client_id, redirect_uri, code_challenge, user_id)
    q = {"code": code}
    if state:
        q["state"] = state
    await _redirect(send, f"{redirect_uri}?{urllib.parse.urlencode(q)}")


async def _token(receive, send) -> None:
    form = urllib.parse.parse_qs((await _read_body(receive)).decode())

    def f(name: str) -> str:
        return form.get(name, [""])[0]

    grant = f("grant_type")
    if grant == "refresh_token":
        result = refresh_access(f("refresh_token"))
        if not result:
            await _json(send, 400, {"error": "invalid_grant"})
            return
        access, expires_in = result
        await _json(
            send, 200,
            {"access_token": access, "token_type": "Bearer",
             "expires_in": expires_in, "scope": "mcp"},
            {"cache-control": "no-store"},
        )
        return

    if grant != "authorization_code":
        await _json(send, 400, {"error": "unsupported_grant_type"})
        return

    def bad(desc: str):
        return _json(send, 400, {"error": "invalid_grant", "error_description": desc})

    data = consume_code(f("code"))
    if not data:
        await bad("bad or expired code")
        return
    if f("client_id") and f("client_id") != data["client_id"]:
        await bad("client mismatch")
        return
    if f("redirect_uri") != data["redirect_uri"]:
        await bad("redirect_uri mismatch")
        return
    if not verify_pkce(f("code_verifier"), data["code_challenge"]):
        await bad("PKCE verification failed")
        return

    access, refresh, expires_in = issue_tokens(data["user_id"])
    await _json(
        send, 200,
        {"access_token": access, "token_type": "Bearer", "expires_in": expires_in,
         "refresh_token": refresh, "scope": "mcp"},
        {"cache-control": "no-store"},
    )


def _login_page(params: dict, error: str | None = None) -> str:
    """Self-contained Arte Suave login page carrying the OAuth params forward."""
    esc = _html_escape
    hidden = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(params.get(k, ""))}">'
        for k in _FORWARD_PARAMS
    )
    err = f'<p class="error">{esc(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="da">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Log ind — Arte Suave</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: #0f1115; color: #f2f3f5; padding: 24px;
  }}
  .card {{
    width: 100%; max-width: 380px; background: #181b21; border: 1px solid #262a33;
    border-radius: 16px; padding: 32px; box-shadow: 0 12px 40px rgba(0,0,0,.4);
  }}
  h1 {{ font-size: 1.25rem; margin: 0 0 4px; }}
  p.sub {{ margin: 0 0 24px; color: #9aa1ad; font-size: .9rem; }}
  label {{ display: block; font-size: .8rem; color: #c3c9d4; margin: 16px 0 6px; }}
  input[type=email], input[type=password] {{
    width: 100%; padding: 11px 13px; border-radius: 10px; border: 1px solid #303541;
    background: #0f1115; color: #f2f3f5; font-size: 1rem;
  }}
  input:focus {{ outline: 2px solid #d34; border-color: #d34; }}
  button {{
    width: 100%; margin-top: 24px; padding: 12px; border: 0; border-radius: 10px;
    background: #d33a3a; color: #fff; font-size: 1rem; font-weight: 600; cursor: pointer;
  }}
  button:hover {{ background: #e04545; }}
  .error {{
    background: #3a1618; border: 1px solid #6a2429; color: #ffb4b4;
    padding: 10px 12px; border-radius: 10px; font-size: .85rem; margin: 16px 0 0;
  }}
  .foot {{ margin-top: 20px; font-size: .75rem; color: #6c727e; line-height: 1.5; }}
</style>
</head>
<body>
  <form class="card" method="post" action="/authorize" autocomplete="on">
    <h1>Arte Suave</h1>
    <p class="sub">Log ind med din Arte Suave-konto for at forbinde Claude.</p>
    {err}
    {hidden}
    <label for="email">Email</label>
    <input id="email" name="email" type="email" required autofocus autocomplete="username">
    <label for="password">Adgangskode</label>
    <input id="password" name="password" type="password" required autocomplete="current-password">
    <button type="submit">Log ind</button>
    <p class="foot">Dine loginoplysninger sendes kun til Arte Suave for at bekræfte
      din konto og gemmes krypteret, så forbindelsen kan forny sig selv. De vises
      aldrig for andre.</p>
  </form>
</body>
</html>"""


def _html_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
