"""Tool logic, framework-agnostic. server.py wraps these as MCP tools.

Every function returns a plain dict envelope (models.ok / parse_failed / error)
so failures are always structured and never opaque.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import time

from . import config, creds
from .client import AuthError, PortalClient, WAFError
from .models import error, ok, parse_failed
from .parsers import (
    ParseError,
    find_csrf_for,
    parse_attendance,
    parse_bookings,
    parse_schedule,
)
from .session_store import SessionStore, build_backend

# The authenticated user for the current request. Set by the server's auth guard
# before it dispatches, so tool signatures the model sees stay identity-free.
_current_user: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user", default=creds.DEFAULT_USER
)

_clients: dict[str, PortalClient] = {}
# keyed by (user_id, day) so one user's booking state never bleeds into another's
_schedule_cache: dict[tuple[str, str], tuple[float, str]] = {}


def set_current_user(user_id: str) -> contextvars.Token:
    return _current_user.set(user_id)


def reset_current_user(token: contextvars.Token) -> None:
    _current_user.reset(token)


def get_client() -> PortalClient:
    user_id = _current_user.get()
    client = _clients.get(user_id)
    if client is None:
        key = "session" if user_id == creds.DEFAULT_USER else f"session#{user_id}"
        store = SessionStore(build_backend(key=key))
        client = PortalClient(lambda u=user_id: creds.get_credentials(u), store)
        _clients[user_id] = client
    return client


def _today() -> dt.date:
    return dt.date.today()


def _daterange(date_from: str | None, date_to: str | None) -> list[str]:
    start = dt.date.fromisoformat(date_from) if date_from else _today()
    end = dt.date.fromisoformat(date_to) if date_to else start
    if end < start:
        start, end = end, start
    days = (end - start).days + 1
    days = min(days, config.MAX_SCHEDULE_DAYS)
    return [(start + dt.timedelta(days=i)).isoformat() for i in range(days)]


def _fetch_schedule_day(day: str) -> str:
    now = time.monotonic()
    ckey = (_current_user.get(), day)
    cached = _schedule_cache.get(ckey)
    if cached and now - cached[0] < config.SCHEDULE_CACHE_TTL:
        return cached[1]
    url = f"{config.ACCOUNT}{config.Q_SCHEDULE}&{config.FIELD['start_date']}={day}"
    html = get_client().get_authed(url)
    _schedule_cache[ckey] = (now, html)
    return html


# -- tools --------------------------------------------------------------------
def get_schedule(
    discipline: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    group = config.resolve_discipline(discipline)
    if discipline and group is None:
        # Unknown discipline: don't silently return everything.
        return ok(
            [],
            note=f"'{discipline}' matched no known discipline group",
            known_groups=list(config.DISCIPLINE_ALIASES),
        )
    classes: list[dict] = []
    days = _daterange(date_from, date_to)
    for day in days:
        try:
            html = _fetch_schedule_day(day)
        except (AuthError, WAFError) as e:
            return error("fetch_schedule", str(e))
        try:
            rows = parse_schedule(html, date=day)
        except ParseError as e:
            return parse_failed(e.step, e.expected, html, detail=e.detail)
        for c in rows:
            if group and not config.class_matches_discipline(c.name, group):
                continue
            classes.append(c.model_dump())
    return ok(
        classes,
        matched_discipline=group,
        days=days,
        filtered=bool(group),
    )


def get_my_bookings() -> dict:
    url = f"{config.ACCOUNT}{config.Q_BOOKINGS}"
    try:
        html = get_client().get_authed(url)
    except (AuthError, WAFError) as e:
        return error("fetch_bookings", str(e))
    try:
        bookings = parse_bookings(html)
    except ParseError as e:
        return parse_failed(e.step, e.expected, html, detail=e.detail)
    return ok([b.model_dump() for b in bookings])


def get_history(date_from: str | None = None, date_to: str | None = None) -> dict:
    """Attendance summary. The portal exposes aggregates + a per-discipline
    breakdown on the Stats page rather than a dated per-session list."""
    url = f"{config.ACCOUNT}{config.Q_STATS}"
    try:
        html = get_client().get_authed(url)
    except (AuthError, WAFError) as e:
        return error("fetch_history", str(e))
    try:
        summary = parse_attendance(html)
    except ParseError as e:
        return parse_failed(e.step, e.expected, html, detail=e.detail)
    return ok(
        summary.model_dump(),
        note="portal exposes attendance as aggregates + per-discipline totals, "
        "not a dated per-session log",
    )


def book_class(class_id: str) -> dict:
    return _signup(class_id, config.FIELD["book_value"], "book_class")


def cancel_booking(booking_id: str) -> dict:
    return _signup(booking_id, config.FIELD["cancel_value"], "cancel_booking")


def _signup(work_schedule_id: str, action_value: str, step: str) -> dict:
    """Find the fresh csrf for the class across the schedule window, then POST.

    Booking/cancel needs the per-render csrf that lives on the class's own row.
    """
    client = get_client()
    # search today .. +MAX days for the row carrying this WorkScheduleID
    window_end = (_today() + dt.timedelta(days=config.MAX_SCHEDULE_DAYS - 1)).isoformat()
    for day in _daterange(_today().isoformat(), window_end):
        try:
            html = _fetch_schedule_day(day)
        except (AuthError, WAFError) as e:
            return error(step, str(e))
        found = find_csrf_for(html, work_schedule_id)
        if not found:
            continue
        csrf, start_date = found
        data = {
            config.FIELD["csrf"]: csrf,
            config.FIELD["action"]: config.FIELD["action_value"],
            config.FIELD["start_date"]: start_date or day,
            config.FIELD["signup_action"]: action_value,
            config.FIELD["work_schedule_id"]: str(work_schedule_id),
        }
        url = (
            f"{config.ACCOUNT}{config.Q_SCHEDULE}"
            f"&{config.FIELD['start_date']}={start_date or day}"
        )
        try:
            resp = client.post_authed(url, data)
        except (AuthError, WAFError) as e:
            return error(step, str(e))
        _schedule_cache.pop((_current_user.get(), day), None)  # invalidate; spots changed
        body = None
        try:
            body = resp.json()
        except ValueError:
            pass
        result = {"class_id": str(work_schedule_id), "action": action_value}
        if body is not None:
            if body.get("ok") is False:
                return error(step, body.get("message", "portal rejected the request"))
            return ok(result, message=body.get("message"))
        # HTML response: caller should confirm via get_my_bookings
        return ok(
            result,
            message="submitted; confirm via get_my_bookings",
            http_status=resp.status_code,
        )
    return error(
        step,
        f"class {work_schedule_id} not found in the next {config.MAX_SCHEDULE_DAYS} days",
    )


def debug_fetch(target: str) -> dict:
    """Return sanitized raw HTML for a named target so the harness can adapt."""
    targets = {
        "schedule": f"{config.ACCOUNT}{config.Q_SCHEDULE}",
        "my_bookings": f"{config.ACCOUNT}{config.Q_BOOKINGS}",
        "history": f"{config.ACCOUNT}{config.Q_STATS}",
        "membership": f"{config.ACCOUNT}{config.Q_MEMBERSHIP}",
    }
    url = targets.get(target)
    if not url:
        return error("debug_fetch", f"unknown target '{target}'", raw=str(list(targets)))
    try:
        html = get_client().get_authed(url)
    except (AuthError, WAFError) as e:
        return error("debug_fetch", str(e))
    return ok(
        {"target": target, "url": url, "length": len(html)},
        raw_excerpt=_sanitize(html)[: config.RAW_EXCERPT_MAX],
        raw_truncated=len(html) > config.RAW_EXCERPT_MAX,
    )


def _sanitize(html: str) -> str:
    """Strip the notification/inbox regions (personal messages) and any token
    strings before returning raw HTML."""
    import re

    # drop the notification bell dropdowns (contain personal message previews)
    html = re.sub(
        r'<div class="md-bell-dropdown".*?</div></div></div>',
        "<!--bell-->",
        html,
        flags=re.S,
    )
    html = re.sub(r'name="csrf" value="[0-9a-f]+"', 'name="csrf" value="***"', html)
    html = re.sub(r"(PHPSESSID=)[^;&\s\"']+", r"\1***", html)
    return html


def health_check() -> dict:
    """Verify login + that each parser still returns sane data. Per-endpoint."""
    results: dict[str, dict] = {}
    today = _today().isoformat()

    def probe(name, fetch, parse):
        try:
            html = fetch()
        except (AuthError, WAFError) as e:
            return {"status": "error", "detail": str(e)}
        try:
            data = parse(html)
            return {"status": "ok", "summary": data}
        except ParseError as e:
            return {"status": "parse_failed", "step": e.step, "expected": e.expected}

    results["auth"] = {"status": "unknown"}
    results["schedule"] = probe(
        "schedule",
        lambda: _fetch_schedule_day(today),
        lambda h: {"rows": len(parse_schedule(h, today))},
    )
    results["auth"] = (
        {"status": "ok"}
        if results["schedule"]["status"] != "error"
        else {"status": "error", "detail": results["schedule"].get("detail")}
    )
    results["my_bookings"] = probe(
        "my_bookings",
        lambda: get_client().get_authed(f"{config.ACCOUNT}{config.Q_BOOKINGS}"),
        lambda h: {"rows": len(parse_bookings(h))},
    )
    results["history"] = probe(
        "history",
        lambda: get_client().get_authed(f"{config.ACCOUNT}{config.Q_STATS}"),
        lambda h: {"total": parse_attendance(h).total},
    )
    overall = "ok" if all(v.get("status") == "ok" for v in results.values()) else "degraded"
    return ok(results, overall=overall)
