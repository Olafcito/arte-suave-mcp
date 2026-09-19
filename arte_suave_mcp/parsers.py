"""HTML parsing, isolated. All selectors come from config.SEL/FIELD.

Each parser returns either a list/model or raises ParseError, which the tool
layer converts into a structured parse_failed result with a raw excerpt.
"""

from __future__ import annotations

import datetime as dt
import re

from selectolax.parser import HTMLParser

from . import config
from .models import AttendanceSummary, Booking, ClassInfo

S = config.SEL
F = config.FIELD


class ParseError(Exception):
    def __init__(self, step: str, expected: str, detail: str | None = None):
        self.step = step
        self.expected = expected
        self.detail = detail
        super().__init__(f"{step}: expected {expected}" + (f" ({detail})" if detail else ""))


def _norm(text: str) -> str:
    """Collapse runs of whitespace to single spaces and strip the ends."""
    return re.sub(r"\s+", " ", text).strip()


def _text(node, sel: str) -> str | None:
    el = node.css_first(sel)
    if el is None:
        return None
    return _norm(el.text()) or None


def _clean_time(raw: str | None) -> str | None:
    """'– 10.00' / '09.00' -> '10:00'."""
    if not raw:
        return None
    m = re.search(r"(\d{1,2})[.:](\d{2})", raw)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _spots(node) -> tuple[int | None, int | None]:
    raw = _text(node, S["spots_count"])
    if not raw:
        return None, None
    m = re.search(r"(\d+)\s*/\s*(\d+)", raw)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _form_fields(node) -> dict[str, str]:
    form = node.css_first(S["signup_form"])
    if form is None:
        return {}
    out = {}
    for inp in form.css("input"):
        name = inp.attributes.get("name")
        if name:
            out[name] = inp.attributes.get("value", "") or ""
    return out


def _parse_row(node, date: str | None) -> ClassInfo:
    name = _text(node, S["name"]) or "Ukendt hold"
    start = _clean_time(_text(node, S["time_start"]))
    end = _clean_time(_text(node, S["time_end"]))
    fields = _form_fields(node)
    class_id = node.attributes.get(F["row_id_attr"]) or fields.get(F["work_schedule_id"]) or ""
    row_html = node.html or ""
    booking_open = not any(m in row_html for m in config.CLOSED_MARKERS)
    avail, cap = _spots(node)
    group = _discipline_group(name)
    start_dt = f"{date} {start}" if date and start else start
    end_dt = f"{date} {end}" if date and end else end
    return ClassInfo(
        class_id=str(class_id),
        name=name,
        discipline_group=group,
        trainer=_text(node, S["instructor"]),
        start=start_dt,
        end=end_dt,
        date=date,
        location=_text(node, S["area"]),
        description=_text(node, S["desc"]),
        spots_available=avail,
        capacity=cap,
        bookable=bool(fields.get(F["signup_action"]) == F["book_value"]),
        # an already-booked class renders an unregister form instead of a signup form
        signed_up=bool(fields.get(F["signup_action"]) == F["cancel_value"]),
        booking_open=booking_open,
    )


def parse_schedule(html: str, date: str | None = None) -> list[ClassInfo]:
    tree = HTMLParser(html)
    rows = tree.css(S["row"])
    if not rows:
        # An empty day is legitimate ("Ingen hold denne dag"); distinguish it
        # from a structural break by checking the page rendered the training UI.
        if S["main"].lstrip(".") in html or "mu-empty" in html or "member-training" in html:
            return []
        raise ParseError("schedule", f"at least one '{S['row']}' or the training UI")
    return [_parse_row(r, date) for r in rows]


def _discipline_group(name: str) -> str | None:
    for g in config.DISCIPLINE_ALIASES:
        if config.class_matches_discipline(name, g):
            return g
    return None


def _public_day_date(heading: str) -> str | None:
    """'Monday 21 Sep 2026' -> '2026-09-21'. None if it isn't a day heading."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\s+(\d{4})", heading)
    if not m:
        return None
    month = config.PUBLIC_MONTHS.get(m.group(2).lower())
    if not month:
        return None
    return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"


def _public_class(cells, date: str, location: str | None) -> ClassInfo | None:
    times = re.findall(r"(\d{1,2}:\d{2})", cells[0].text())
    name_el = cells[1].css_first(config.PUBLIC_SEL["class_link"])
    name = _norm(name_el.text() if name_el else cells[1].text())
    if not name:
        return None
    start = times[0] if times else None
    end = times[1] if len(times) > 1 else None
    trainer = _norm(cells[2].text()) if len(cells) > 2 else None
    return ClassInfo(
        class_id="",  # public schedule carries no bookable WorkScheduleID
        name=name,
        discipline_group=_discipline_group(name),
        trainer=trainer or None,
        start=f"{date} {start}" if start else None,
        end=f"{date} {end}" if end else None,
        date=date,
        location=location,
        spots_available=None,
        capacity=None,
        bookable=False,
        booking_open=False,
        source="schedule",
    )


def parse_public_week(html: str) -> dict[str, list[ClassInfo]]:
    """Parse the public weekly schedule into {YYYY-MM-DD: [ClassInfo]}.

    The page is a flat run of headings and tables: an <h1> starts a day, an <h5>
    names the mat/location, and each following table lists that mat's classes.
    We walk the document in order, tracking the current day and location.
    """
    tree = HTMLParser(html)
    root = tree.body or tree.root
    day_sel = config.PUBLIC_SEL["day_heading"]
    mat_sel = config.PUBLIC_SEL["mat_heading"]
    table_sel = config.PUBLIC_SEL["table"]
    table_class = config.PUBLIC_SEL["table_class"]
    out: dict[str, list[ClassInfo]] = {}
    current_day: str | None = None
    current_loc: str | None = None
    saw_table = False
    for node in root.traverse(include_text=False):
        tag = node.tag
        if tag == day_sel:
            current_day = _public_day_date(_norm(node.text()))
            current_loc = None
        elif tag == mat_sel:
            current_loc = _norm(node.text()) or None
        elif tag == table_sel and table_class in (node.attributes.get("class") or ""):
            saw_table = True
            if current_day is None:
                continue
            out.setdefault(current_day, [])
            for row in node.css(config.PUBLIC_SEL["row"]):
                cells = row.css(config.PUBLIC_SEL["cell"])
                if len(cells) < 2:
                    continue  # header row (uses <th>) or spacer
                ci = _public_class(cells, current_day, current_loc)
                if ci is not None:
                    out[current_day].append(ci)
    if not saw_table and "traeningstider" not in html.lower():
        raise ParseError("public_schedule", "at least one weekly schedule table")
    return out


def _label_date(text: str, today: dt.date) -> str | None:
    """'lørdag 19.09.' -> ISO date. The label has no year, so pick the calendar
    year that puts the date nearest to `today` (handles the Dec/Jan boundary)."""
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.", text)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    best: dt.date | None = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            cand = dt.date(year, month, day)
        except ValueError:
            continue  # e.g. 29.02. in a non-leap year
        if best is None or abs((cand - today).days) < abs((best - today).days):
            best = cand
    return best.isoformat() if best else None


def parse_bookings(html: str, today: dt.date | None = None) -> list[Booking]:
    """Parse the bookings page. Rows are grouped under day labels
    (``md-class-list__day-label``); we carry each label's date onto its rows so
    every booking knows which day it is on."""
    today = today or dt.date.today()
    label_token = S["day_label"].lstrip(".")
    row_token = S["row"].lstrip(".")
    tree = HTMLParser(html)
    root = tree.body or tree.root
    out: list[Booking] = []
    current_date: str | None = None
    for node in root.traverse(include_text=False):
        tokens = (node.attributes.get("class") or "").split()
        if label_token in tokens:
            current_date = _label_date(node.text(), today)
        elif row_token in tokens:
            ci = _parse_row(node, current_date)
            out.append(Booking(**ci.model_dump(), booking_id=ci.class_id))
    # An empty bookings list is valid; only fail if the page didn't render at all.
    if not out and "member-training" not in html and "mu-" not in html:
        raise ParseError("bookings", "the bookings UI region")
    return out


def parse_attendance(html: str) -> AttendanceSummary:
    tree = HTMLParser(html)
    # tree.text() strips tags for both full documents and fragments (body-less),
    # so "Seneste træning: <strong>19.09.2026</strong> – Thai" flattens cleanly.
    flat = re.sub(r"\s+", " ", tree.text())

    def num_before(label: str) -> int | None:
        m = re.search(r"(\d+)\s*" + re.escape(label), flat)
        return int(m.group(1)) if m else None

    summary = AttendanceSummary(
        this_month=num_before("træninger") if "Denne måned" in flat else None,
        total=None,
    )
    # Structured counts around their Danish labels.
    m_month = re.search(r"Denne måned\s*(\d+)", flat)
    m_30 = re.search(r"Seneste 30 dage\s*(\d+)", flat)
    m_total = re.search(r"I alt\s*(\d+)\s*træninger", flat)
    m_hours = re.search(r"Timer\s*([\d.,]+)", flat)
    m_latest = re.search(
        r"Seneste træning:\s*([\d.]{6,})\s*[^\w]{1,3}\s*([^<.]+?)(?:\s*Se |$)", flat
    )
    if m_month:
        summary.this_month = int(m_month.group(1))
    if m_30:
        summary.last_30_days = int(m_30.group(1))
    if m_total:
        summary.total = int(m_total.group(1))
    if m_hours:
        summary.total_hours = float(m_hours.group(1).replace(",", "."))
    if m_latest:
        summary.latest_date = m_latest.group(1).strip()
        summary.latest_class = m_latest.group(2).strip()

    # Per-discipline breakdown from the Chart.js arrays in inline JS.
    labels = re.search(r"kLabels\s*=\s*\[([^\]]*)\]", html)
    data = re.search(r"kData\s*=\s*\[([^\]]*)\]", html)
    if labels and data:
        names = re.findall(r'"([^"]+)"', labels.group(1))
        counts = [int(x) for x in re.findall(r"\d+", data.group(1))]
        summary.by_discipline = dict(zip(names, counts, strict=False))

    if summary.total is None and not summary.by_discipline:
        raise ParseError("attendance", "at least the all-time training count")
    return summary


def find_csrf_for(html: str, work_schedule_id: str) -> tuple[str, str] | None:
    """Locate the fresh csrf + StartDate for a given class row on a schedule page.

    Returns (csrf, start_date) or None if the row/form isn't present.
    """
    tree = HTMLParser(html)
    for row in tree.css(S["row"]):
        if row.attributes.get(F["row_id_attr"]) == str(work_schedule_id):
            fields = _form_fields(row)
            if F["csrf"] in fields:
                return fields[F["csrf"]], fields.get(F["start_date"], "")
    return None
