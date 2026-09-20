"""Single source of truth for everything that changes when the site changes:
endpoints, selectors, field names, discipline aliases and category mapping.

Tool code and parsers must import from here — never hard-code a URL or a CSS
selector anywhere else.
"""

from __future__ import annotations

BASE = "https://am.artesuave.dk"
PORTAL_ENTRY = f"{BASE}/a/artesuave/webshop"
ACCOUNT = f"{BASE}/webshop/Account/index.php"

# --- Public weekly schedule (artesuave.dk marketing site, no auth) -----------
# The member portal only returns today + the weeks the gym has released (past
# days and unreleased weeks come back empty; next week is released on Sunday).
# The public WordPress schedule publishes every week,
# past and future, keyed by that week's Monday. It carries no live spots/booking,
# so classes from here are the *planned* schedule (subject to change).
PUBLIC_BASE = "https://artesuave.dk"
PUBLIC_SCHEDULE = f"{PUBLIC_BASE}/traeningstider/"
PUBLIC_START_PARAM = "StartDate"  # value is a Monday, formatted DD-MM-YYYY
PUBLIC_WAF_VERIFY_URL = f"{PUBLIC_BASE}/.sc-verify/"
PUBLIC_WAF_COOKIE_DOMAIN = "artesuave.dk"
PUBLIC_SCHEDULE_CACHE_TTL = 3600  # public plan changes rarely; cache a week for 1h

# --- WAF (simply.com proof-of-work) -----------------------------------------
WAF_VERIFY_URL = f"{BASE}/.sc-verify/"
WAF_CHALLENGE_MARKERS = ("Checking your browser", "Security Incident", "sc-challenge")
# regex over the challenge page for T (token), TS (timestamp), D (difficulty bits)
WAF_PARAM_RE = r'var\s+T="([0-9a-f]+)"\s*,\s*TS="(\d+)"\s*,\s*D=(\d+)'
WAF_CLEARANCE_COOKIE = "sc_clearance"

# --- Login -------------------------------------------------------------------
LOGIN_URL = f"{BASE}/ajax/AjaxJson.php?action=LoginUser"
ACCOUNT_ID = "1"  # Arte Suave club id (hidden field on the login form)
# Markers that mean "this HTML is the logged-out login page", i.e. re-login.
LOGGED_OUT_MARKERS = ('name="password"', "PasswordResetRequest")

# --- Data endpoints (query strings appended to ACCOUNT) ----------------------
Q_SCHEDULE = "?Show=ShowProfile&action=SignUpforclasses"
Q_BOOKINGS = "?Show=ShowProfile&action=Bookings"
Q_STATS = "?Show=ShowProfile&action=Stats"
Q_MEMBERSHIP = "?Show=ShowProfile&action=MemberMembership"

# --- Selectors / field names (parsers depend on these) -----------------------
SEL = {
    "row": ".md-class-row",
    "time_start": ".md-class-row__time-start",
    "time_end": ".md-class-row__time-end",
    "name": ".md-class-row__name",
    "area": ".md-class-row__area",
    "desc": ".md-class-row__desc",
    "instructor": ".md-class-row__instructor-name",
    "spots_count": ".md-class-row__spots-count",
    "signup_form": "form.mu-signup-form",
    "main": ".mu-training__main",
    # On the bookings page, each day's rows are preceded by this label
    # ("lørdag 19.09.") — the only place the booking's date appears.
    "day_label": ".md-class-list__day-label",
}
# Public weekly-schedule markup: day is an <h1> ("Monday 21 Sep 2026"), the mat/
# location an <h5>, then a table of classes (Tid / Hold / Instruktører).
PUBLIC_SEL = {
    "day_heading": "h1",
    "mat_heading": "h5",
    "table": "table",
    "table_class": "w3-table",  # class marker on the schedule tables
    "row": "tr",
    "cell": "td",
    "class_link": "a",
}
PUBLIC_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
FIELD = {
    "row_id_attr": "id",  # element id on .md-class-row == WorkScheduleID
    "csrf": "csrf",
    "signup_action": "ClassSignupAction",
    "work_schedule_id": "WorkScheduleID",
    "start_date": "StartDate",
    "action": "action",
    "action_value": "SignUpforclasses",
    "book_value": "signup",
    "cancel_value": "unregister",
}
CLOSED_MARKERS = ("Tilmelding er lukket", "lukket")

# --- Category chips shown on the schedule (server-side kategori[] values) -----
CATEGORIES = [
    "BJJ GI", "BJJ NO-GI", "KICKBOXING", "KIDS", "MMA",
    "OPEN GYM", "OTHER", "WOD", "YOGA",
]

# --- Discipline aliases ------------------------------------------------------
# Maps a canonical group -> the substrings (lowercased) that identify it in the
# gym's original class name. Matching is loose & case-insensitive; the caller
# always gets the gym's original name back plus which group matched.
DISCIPLINE_ALIASES: dict[str, list[str]] = {
    "thai boxing": [
        "thai", "thaiboksning", "thaiboxing", "muay thai", "muaythai",
        "kickboxing", "kick boxing", "kickboksning", "k1", "k-1",
        "motions boksning", "boksning", "boxing",
    ],
    "bjj": ["bjj", "jiu", "jiu-jitsu", "jiujitsu", "gi", "no-gi", "nogi", "grappling"],
    "mma": ["mma", "mixed martial"],
    "wrestling": ["wrestling", "brydning"],
    "wod": ["wod", "conditioning", "strength", "styrke"],
    "yoga": ["yoga", "mobility", "stretch"],
    "open gym": ["open gym", "open mat", "open thaiboksning", "åben", "aben"],
    "kids": ["kids", "børn", "boern", "junior", "ungdom"],
}

# --- HTTP politeness ---------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "da-DK,da;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
REQUEST_TIMEOUT = 30.0
MIN_REQUEST_INTERVAL = 1.0  # seconds between requests to the portal (be polite)
SCHEDULE_CACHE_TTL = 300  # seconds; the day plan barely changes
MAX_SCHEDULE_DAYS = 14  # cap a date-range fan-out

# --- Raw-excerpt safety ------------------------------------------------------
RAW_EXCERPT_MAX = 4000  # cap size of any raw HTML returned to the harness


def resolve_discipline(query: str | None) -> str | None:
    """Return the canonical discipline group for a loose query, or None."""
    if not query:
        return None
    q = query.strip().lower()
    for group, aliases in DISCIPLINE_ALIASES.items():
        if q == group or any(a in q or q in a for a in aliases):
            return group
    return None


def class_matches_discipline(class_name: str, group: str) -> bool:
    """True if the gym's original class name belongs to the canonical group."""
    name = class_name.lower()
    if group in DISCIPLINE_ALIASES:
        return any(a in name for a in DISCIPLINE_ALIASES[group])
    return group.lower() in name
