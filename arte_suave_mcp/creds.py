"""Credential + identity providers. Env locally, SSM SecureString in AWS.

Multi-user model
----------------
Each user has their own Arte Suave login stored under ``/artesuave-mcp/users/<id>/``
(``login``, ``password``, ``token``) as SSM SecureStrings. A user's *bearer token*
is self-identifying: ``<id>.<secret>``. The server splits off ``<id>``, loads that
user's stored secret, and constant-time compares — so we never scan all users and
only ever log the id, never the secret.

Backward compatibility: the legacy single secret (``/artesuave-mcp/mcp-secret``)
still authenticates and maps to the DEFAULT_USER, whose credentials come from the
original ``/artesuave-mcp/{login,password}`` params (or LOCAL env). So an existing
connector keeps working unchanged.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import os
import re
import time

DEFAULT_USER = "__default__"
USERS_PREFIX = "/artesuave-mcp/users"
_UID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Short-lived cache so repeated calls from the same connector don't hit SSM every
# request, and so bad-token spam can't fan out into unbounded SSM lookups.
_IDENTITY_TTL = 300.0
_identity_cache: dict[str, tuple[str | None, float]] = {}


@functools.lru_cache(maxsize=64)
def _ssm_param(name: str) -> str:
    import boto3

    client = boto3.client("ssm")
    resp = client.get_parameter(Name=name, WithDecryption=True)
    return resp["Parameter"]["Value"]


def _ssm_optional(name: str) -> str | None:
    try:
        return _ssm_param(name)
    except Exception:
        return None


def _user_param(user_id: str, field: str) -> str | None:
    return _ssm_optional(f"{USERS_PREFIX}/{user_id}/{field}")


def get_server_secret() -> str | None:
    """The legacy single bearer/path secret (maps to DEFAULT_USER)."""
    param = os.environ.get("ARTESUAVE_MCP_SECRET_PARAM")
    if param:
        return _ssm_optional(param)
    return os.environ.get("ARTESUAVE_MCP_SECRET")


def _resolve_uncached(presented: str) -> str | None:
    # legacy single secret -> default user
    legacy = get_server_secret()
    if legacy and hmac.compare_digest(presented, legacy):
        return DEFAULT_USER
    # per-user "<id>.<secret>"
    if "." in presented:
        user_id, _, secret = presented.partition(".")
        if secret and _UID_RE.match(user_id):
            stored = _user_param(user_id, "token")
            if stored and hmac.compare_digest(secret, stored):
                return user_id
    return None


def resolve_identity(presented: str | None) -> str | None:
    """Map a presented bearer token to a user id, or None if it doesn't match."""
    if not presented:
        return None
    key = hashlib.sha256(presented.encode()).hexdigest()
    now = time.monotonic()
    hit = _identity_cache.get(key)
    if hit and hit[1] > now:
        return hit[0]
    user_id = _resolve_uncached(presented)
    _identity_cache[key] = (user_id, now + _IDENTITY_TTL)
    return user_id


def get_credentials(user_id: str | None = None) -> tuple[str, str]:
    """Return (email, password) for a user. DEFAULT_USER / None uses the legacy
    single-account params (SSM if configured, else env)."""
    if user_id and user_id != DEFAULT_USER:
        login = _user_param(user_id, "login")
        password = _user_param(user_id, "password")
        if not login or not password:
            raise RuntimeError(f"no credentials stored for user '{user_id}'")
        return login, password

    login_param = os.environ.get("ARTESUAVE_LOGIN_PARAM")
    pw_param = os.environ.get("ARTESUAVE_PASSWORD_PARAM")
    if login_param and pw_param:
        return _ssm_param(login_param), _ssm_param(pw_param)
    login = os.environ.get("LOGIN")
    password = os.environ.get("PASSWORD")
    if not login or not password:
        raise RuntimeError(
            "No credentials: set LOGIN/PASSWORD or "
            "ARTESUAVE_LOGIN_PARAM/ARTESUAVE_PASSWORD_PARAM"
        )
    return login, password
