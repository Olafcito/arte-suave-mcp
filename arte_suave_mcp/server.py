"""FastMCP server exposing the Arte Suave tools over streamable HTTP.

Auth: the public Function URL enforces its own secret. We accept it either as a
bearer token (Authorization: Bearer <secret>) or as a secret path segment
(/mcp/<secret>), whichever the connector supports. Set the secret via
ARTESUAVE_MCP_SECRET(_PARAM); if unset (local dev), auth is open.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP

from . import creds, oauth, service
from .creds import get_server_secret

mcp = FastMCP("arte-suave")


@mcp.tool
def get_schedule(
    discipline: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Get Arte Suave classes. `discipline` matches loosely (e.g. "kickboxing",
    "muay thai", "K1" all resolve to thai boxing) and the gym's original class
    name is always returned. Dates are YYYY-MM-DD; omit for today. Returns each
    class with name, discipline_group, trainer, start/end, location, spots."""
    return service.get_schedule(discipline, date_from, date_to)


@mcp.tool
def get_my_bookings() -> dict:
    """Classes you are currently signed up for."""
    return service.get_my_bookings()


@mcp.tool
def get_history(date_from: str | None = None, date_to: str | None = None) -> dict:
    """Your attendance: this-month / 30-day / all-time counts, total hours,
    latest training and a per-discipline breakdown."""
    return service.get_history(date_from, date_to)


@mcp.tool
def book_class(class_id: str) -> dict:
    """Book a class by its class_id (from get_schedule). Writes to your account."""
    return service.book_class(class_id)


@mcp.tool
def cancel_booking(booking_id: str) -> dict:
    """Cancel a booking by its booking_id (from get_my_bookings)."""
    return service.cancel_booking(booking_id)


@mcp.tool
def health_check() -> dict:
    """Verify login and that each parser still returns sane data, per endpoint."""
    return service.health_check()


@mcp.tool
def debug_fetch(target: str) -> dict:
    """Return sanitized raw HTML for a target ("schedule", "my_bookings",
    "history", "membership") so the assistant can adapt if the site changed."""
    return service.debug_fetch(target)


def _base_url(headers: dict) -> str:
    """Public origin of this request, from proxy headers (API Gateway/LWA)."""
    override = os.environ.get("ARTESUAVE_PUBLIC_URL")
    if override:
        return override.rstrip("/")
    proto = headers.get("x-forwarded-proto", "https").split(",")[0].strip()
    host = headers.get("host", "")
    return f"{proto}://{host}"


def _resolve_user(presented: str, path: str, secret: str | None, oauth_on: bool):
    """Map a request to a user id across all supported auth schemes."""
    # OAuth access tokens are self-marked; route them straight to the token store.
    if oauth_on and presented.startswith(oauth.ACCESS_PREFIX):
        return oauth.resolve_access_token(presented)
    user_id = creds.resolve_identity(presented)  # legacy secret + <id>.<secret>
    if user_id is None and secret and f"/{secret}" in path:
        return creds.DEFAULT_USER  # legacy path-secret fallback
    return user_id


def _build_asgi():
    """ASGI app with a per-user auth gate and (optionally) an OAuth server.

    Each request's token resolves to a user id — via the legacy shared secret, a
    self-identifying ``<id>.<secret>`` token, or an OAuth access token — and we
    stash it on a contextvar so the tools act as that user. When OAuth is enabled
    the ``/authorize``, ``/token``, ``/register`` and discovery routes are served
    here, and an unauthenticated MCP request gets a 401 pointing at the metadata
    so the client can start the login flow. With neither a secret nor OAuth
    configured (local dev) auth is open.
    """
    secret = get_server_secret()
    oauth_on = oauth.enabled()
    app = mcp.http_app(path="/mcp")
    if not secret and not oauth_on:
        return app

    from starlette.responses import JSONResponse

    async def guard(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
        path = scope.get("path", "")

        if oauth_on and oauth.is_oauth_path(path):
            await oauth.handle(scope, receive, send, _base_url(headers))
            return

        presented = headers.get("authorization", "").removeprefix("Bearer ").strip()
        user_id = _resolve_user(presented, path, secret, oauth_on)
        if user_id is None:
            extra = {}
            if oauth_on:
                meta = oauth.protected_resource_metadata_url(_base_url(headers))
                extra["WWW-Authenticate"] = f'Bearer resource_metadata="{meta}"'
            resp = JSONResponse({"error": "unauthorized"}, status_code=401, headers=extra)
            await resp(scope, receive, send)
            return
        token = service.set_current_user(user_id)
        try:
            await app(scope, receive, send)
        finally:
            service.reset_current_user(token)

    return guard


app = _build_asgi()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
