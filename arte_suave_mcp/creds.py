"""Credential + server-secret providers. Env locally, SSM SecureString in AWS."""

from __future__ import annotations

import functools
import os


@functools.lru_cache(maxsize=8)
def _ssm_param(name: str) -> str:
    import boto3

    client = boto3.client("ssm")
    resp = client.get_parameter(Name=name, WithDecryption=True)
    return resp["Parameter"]["Value"]


def get_credentials() -> tuple[str, str]:
    """Return (email, password) from SSM if configured, else env."""
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


def get_server_secret() -> str | None:
    """The bearer/path secret the Function URL enforces itself."""
    param = os.environ.get("ARTESUAVE_MCP_SECRET_PARAM")
    if param:
        return _ssm_param(param)
    return os.environ.get("ARTESUAVE_MCP_SECRET")
