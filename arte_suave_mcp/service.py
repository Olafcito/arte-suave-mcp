"""Tool logic, framework-agnostic. server.py wraps these as MCP tools.

Every function returns a plain dict envelope (models.ok / parse_failed / error)
so failures are always structured and never opaque.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import re
import time

from . import config, creds, feedback
from .client import AuthError, PortalClient, WAFError, get_public
from .models import error, ok, parse_failed, sanitize_html
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


def _portal_day(day: str) -> list[dict]:
    """Live class rows the member portal serves for one day. Empty for a day
    with no classes and for days the gym hasn't released yet."""
    html = _fetch_schedule_day(day)
    try:
        rows = parse_schedule(html, date=day)
    except ParseError as e:
        e.html = html  # lets get_schedule return an excerpt of the broken page
        raise
    return [c.model_dump() for c in rows]


def _split_sources(days: list[str]) -> tuple[list[dict], list[str]]:
    """Return (live portal rows, days to serve from the public plan).

    Past days always come from the plan. Today through this week's Sunday is
    always the portal. A future week is released when the portal actually shows
    classes for it — the site decides, not the calendar. Releases are
    sequential, so weeks after the first unreleased one aren't probed."""
    today = _today()
    this_monday = _monday_of(today)
    live: list[dict] = []
    plan_days: list[str] = []
    by_week: dict[dt.date, list[str]] = {}
    for d in days:
        by_week.setdefault(_monday_of(dt.date.fromisoformat(d)), []).append(d)
    unreleased = False
    for monday in sorted(by_week):
        upcoming = [d for d in by_week[monday] if d >= today.isoformat()]
        plan_days += [d for d in by_week[monday] if d not in upcoming]
        if not upcoming:
            continue
        if unreleased:
            plan_days += upcoming
            continue
        rows = [c for d in upcoming for c in _portal_day(d)]
        if rows or monday == this_monday:
            live += rows
        else:
            unreleased = True
            plan_days += upcoming
    return live, sorted(plan_days)


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


# Fields that are server plumbing, not schedule information. Planned rows (the
# public weekly plan) additionally lose every booking-ish field: booking for
# those days simply hasn't opened, which the response says once at the top
# instead of as noisy per-row false/null values.
_INTERNAL_FIELDS = ("source", "booking_open")
_PLANNED_HIDDEN = _INTERNAL_FIELDS + (
    "class_id", "bookable", "signed_up", "spots_available", "capacity",
)


def _present_class(c: dict) -> dict:
    """Shape one class row for output: keep only fields that carry meaning."""
    hidden = _PLANNED_HIDDEN if c.get("source") != "portal" else _INTERNAL_FIELDS
    out = {k: v for k, v in c.items() if k not in hidden and v is not None}
    if c.get("source") == "portal" and not c.get("booking_open", True):
        out["booking_open"] = False  # closed signup is worth surfacing
    return out


def _schedule_notes(plan_days: list[str]) -> str | None:
    """Plain-language provenance for days the portal doesn't serve live."""
    today = _today().isoformat()
    notes = []
    if any(d < today for d in plan_days):
        notes.append("Past days shown are from the gym's public weekly plan.")
    future = sorted(d for d in plan_days if d > today)
    if future:
        release = _monday_of(dt.date.fromisoformat(future[0])) - dt.timedelta(days=1)
        note = f"Classes from {future[0]} onward have not been released for booking yet"
        if release > _today():
            note += (
                "; the next batch is expected to open Sunday "
                f"{release.strftime('%d-%m-%Y')}"
            )
        elif release == _today():
            note += "; they are expected to open later today"
        notes.append(note + ".")
    return " ".join(notes) or None


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
    try:
        classes, plan_days = _split_sources(days)
    except (AuthError, WAFError) as e:
        return error("fetch_schedule", str(e))
    except ParseError as e:
        return parse_failed(e.step, e.expected, e.html, detail=e.detail)

    if classes:
        # Cross-check bookings so each live row says signed_up directly and the
        # caller never has to reconcile against get_my_bookings. Best-effort:
        # the parser-derived flag stands if the bookings fetch fails.
        try:
            booked = _booked_ids()
        except (AuthError, WAFError, ParseError):
            booked = None
        if booked is not None:
            for c in classes:
                c["signed_up"] = c["class_id"] in booked

    if plan_days:
        try:
            week = _public_days(plan_days)
        except (AuthError, WAFError) as e:
            return error("fetch_public_schedule", str(e))
        except ParseError as e:
            return parse_failed(e.step, e.expected, "", detail=e.detail)
        for day in plan_days:
            classes.extend(week[day])

    if group:
        classes = [c for c in classes if config.class_matches_discipline(c["name"], group)]
    classes.sort(key=lambda c: (c.get("date") or "", c.get("start") or ""))

    extra: dict = {"matched_discipline": group, "days": days, "filtered": bool(group)}
    note = _schedule_notes(plan_days)
    if note:
        extra["note"] = note
    return ok([_present_class(c) for c in classes], **extra)


def get_my_bookings() -> dict:
    url = f"{config.ACCOUNT}{config.Q_BOOKINGS}"
    try:
        html = get_client().get_authed(url)
    except (AuthError, WAFError) as e:
        return error("fetch_bookings", str(e))
    try:
        bookings = parse_bookings(html, today=_today())
    except ParseError as e:
        return parse_failed(e.step, e.expected, html, detail=e.detail)
    hidden = _INTERNAL_FIELDS + ("bookable",)  # 'bookable' is noise on a booking
    rows = [
        {k: v for k, v in b.model_dump().items() if k not in hidden and v is not None}
        for b in bookings
    ]
    return ok(rows)


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
    clean = sanitize_html(html)
    # Start at the training UI when the page has one; the head/nav alone would
    # otherwise fill the whole excerpt.
    main = re.search(
        r'<[^<>]*class="[^"]*' + re.escape(config.SEL["main"].lstrip(".")), clean
    )
    offset = main.start() if main else 0
    return ok(
        {"target": target, "url": url, "length": len(html)},
        raw_excerpt=clean[offset : offset + config.RAW_EXCERPT_MAX],
        raw_offset=offset,
        raw_truncated=len(clean) - offset > config.RAW_EXCERPT_MAX,
    )


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
