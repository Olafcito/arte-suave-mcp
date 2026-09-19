"""Tool logic, framework-agnostic. server.py wraps these as MCP tools.

Every function returns a plain dict envelope (models.ok / parse_failed / error)
so failures are always structured and never opaque.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import time

from . import config, creds, feedback
from .client import AuthError, PortalClient, WAFError, get_public
from .models import error, ok, parse_failed
from .parsers import (
    ParseError,
    find_csrf_for,
    parse_attendance,
    parse_bookings,
    parse_public_week,
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
# public weekly plan is identical for everyone -> cache by week-start (Monday iso)
_public_week_cache: dict[str, tuple[float, dict[str, list]]] = {}


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


def _use_portal(day: dt.date) -> bool:
    """True for days the member portal actually serves with live data: today
    through the end of the current week. Past days (even this week) and future
    weeks come from the public plan instead."""
    today = _today()
    week_end = today + dt.timedelta(days=6 - today.weekday())  # Sunday of this week
    return today <= day <= week_end


def _monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _fetch_public_week(monday: dt.date) -> dict[str, list]:
    """Fetch + parse the public plan for the week starting `monday` (cached)."""
    key = monday.isoformat()
    now = time.monotonic()
    cached = _public_week_cache.get(key)
    if cached and now - cached[0] < config.PUBLIC_SCHEDULE_CACHE_TTL:
        return cached[1]
    param = monday.strftime("%d-%m-%Y")
    url = f"{config.PUBLIC_SCHEDULE}?{config.PUBLIC_START_PARAM}={param}"
    html = get_public(url)
    week = {d: [c.model_dump() for c in rows] for d, rows in parse_public_week(html).items()}
    _public_week_cache[key] = (now, week)
    return week


def _public_days(days: list[str]) -> dict[str, list]:
    """Return {day: [class dicts]} for the given ISO days from the public plan,
    fetching each distinct week only once."""
    result: dict[str, list] = {}
    weeks = {_monday_of(dt.date.fromisoformat(d)) for d in days}
    for monday in sorted(weeks):
        result.update(_fetch_public_week(monday))
    return {d: result.get(d, []) for d in days}


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
    days = _daterange(date_from, date_to)
    # Split by source: the portal has live data only for today..end-of-week;
    # everything else (past days, future weeks) comes from the public plan.
    portal_days = [d for d in days if _use_portal(dt.date.fromisoformat(d))]
    plan_days = [d for d in days if d not in portal_days]

    classes: list[dict] = []
    for day in portal_days:
        try:
            html = _fetch_schedule_day(day)
        except (AuthError, WAFError) as e:
            return error("fetch_schedule", str(e))
        try:
            rows = parse_schedule(html, date=day)
        except ParseError as e:
            return parse_failed(e.step, e.expected, html, detail=e.detail)
        classes.extend(c.model_dump() for c in rows)

    planned = 0
    if plan_days:
        try:
            week = _public_days(plan_days)
        except (AuthError, WAFError) as e:
            return error("fetch_public_schedule", str(e))
        except ParseError as e:
            return parse_failed(e.step, e.expected, "", detail=e.detail)
        for day in plan_days:
            classes.extend(week[day])
            planned += len(week[day])

    if group:
        classes = [c for c in classes if config.class_matches_discipline(c["name"], group)]
    classes.sort(key=lambda c: (c.get("date") or "", c.get("start") or ""))

    extra: dict = {"matched_discipline": group, "days": days, "filtered": bool(group)}
    if planned:
        extra["note"] = (
            "Some classes have source='schedule': they come from Arte Suave's "
            "public weekly plan (past days and future weeks). They may change and "
            "have no live spots or booking. Live spots/booking exist only on "
            "source='portal' (this week's upcoming classes)."
        )
    return ok(classes, **extra)


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
    return _signup(class_id, config.FIELD["book_value"], "book_class", expect_booked=True)


def cancel_booking(booking_id: str) -> dict:
    return _signup(
        booking_id, config.FIELD["cancel_value"], "cancel_booking", expect_booked=False
    )


def _booked_ids() -> set[str]:
    """The WorkScheduleIDs currently in the user's bookings (fresh fetch)."""
    html = get_client().get_authed(f"{config.ACCOUNT}{config.Q_BOOKINGS}")
    return {b.class_id for b in parse_bookings(html)}


def _confirm_signup(work_schedule_id: str, expect_booked: bool, step: str) -> dict:
    """Verify a book/cancel actually took by reading get_my_bookings back."""
    wsid = str(work_schedule_id)
    try:
        present = wsid in _booked_ids()
    except (AuthError, WAFError) as e:
        return error(step, f"submitted but could not confirm via get_my_bookings: {e}")
    except ParseError as e:
        return parse_failed(e.step, e.expected, "", detail=e.detail)
    if expect_booked and not present:
        return error(step, "submitted, but the class did not appear in your bookings")
    if not expect_booked and present:
        return error(step, "submitted, but the class is still in your bookings")
    return ok(
        {"class_id": wsid, "booked": present, "confirmed": True},
        message="booked" if expect_booked else "cancelled",
    )


def _signup(
    work_schedule_id: str, action_value: str, step: str, *, expect_booked: bool
) -> dict:
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
        if body is not None and body.get("ok") is False:
            return error(step, body.get("message", "portal rejected the request"))
        # Always confirm the write actually landed by reading bookings back.
        return _confirm_signup(work_schedule_id, expect_booked, step)
    return error(
        step,
        f"class {work_schedule_id} not found in the next {config.MAX_SCHEDULE_DAYS} days",
    )


def submit_feedback(message: str, context: str | None = None) -> dict:
    """Record feedback (e.g. something the user disagreed with) for later review."""
    if not (message or "").strip():
        return error("submit_feedback", "message is required")
    try:
        rec = feedback.store(_current_user.get(), message, context)
    except Exception as e:  # storage should never crash the tool
        return error("submit_feedback", f"could not store feedback: {e}")
    return ok(rec, message="feedback recorded")


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
    monday = _monday_of(_today())
    results["public_schedule"] = probe(
        "public_schedule",
        lambda: get_public(
            f"{config.PUBLIC_SCHEDULE}?{config.PUBLIC_START_PARAM}={monday.strftime('%d-%m-%Y')}"
        ),
        lambda h: {"days": len(parse_public_week(h))},
    )
    overall = "ok" if all(v.get("status") == "ok" for v in results.values()) else "degraded"
    return ok(results, overall=overall)
