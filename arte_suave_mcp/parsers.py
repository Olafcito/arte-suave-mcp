"""HTML parsing, isolated. All selectors come from config.SEL/FIELD.

Each parser returns either a list/model or raises ParseError, which the tool
layer converts into a structured parse_failed result with a raw excerpt.
"""

from __future__ import annotations

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


def _text(node, sel: str) -> str | None:
    el = node.css_first(sel)
    if el is None:
        return None
    return re.sub(r"\s+", " ", el.text()).strip() or None


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
    group = None
    for g in config.DISCIPLINE_ALIASES:
        if config.class_matches_discipline(name, g):
            group = g
            break
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


def parse_bookings(html: str) -> list[Booking]:
    tree = HTMLParser(html)
    rows = tree.css(S["row"])
    out: list[Booking] = []
    for r in rows:
        ci = _parse_row(r, None)
        out.append(Booking(**ci.model_dump(), booking_id=ci.class_id))
    # An empty bookings list is valid; only fail if the page didn't render at all.
    if not out and "member-training" not in html and "mu-" not in html:
        raise ParseError("bookings", "the bookings UI region")
    return out


def parse_attendance(html: str) -> AttendanceSummary:
    tree = HTMLParser(html)
    flat = re.sub(r"\s+", " ", (tree.body.text() if tree.body else html))

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
    m_latest = re.search(r"Seneste træning:\s*([\d.]+)\s*[–\-—]\s*([^.]+?)(?:Se|$)", flat)
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
