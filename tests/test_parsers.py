from pathlib import Path

import pytest

from arte_suave_mcp import config
from arte_suave_mcp.parsers import (
    ParseError,
    find_csrf_for,
    parse_attendance,
    parse_bookings,
    parse_public_week,
    parse_schedule,
)

FIX = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_parse_schedule_rows():
    classes = parse_schedule(_read("schedule_day.html"), date="2026-09-20")
    assert len(classes) == 9
    c = classes[0]
    assert c.class_id == "48066"
    assert c.name == "Big Boys"
    assert c.start == "2026-09-20 09:00"
    assert c.end == "2026-09-20 10:00"
    assert c.capacity == 46
    assert c.spots_available == 46
    assert c.location and "Mat 2" in c.location
    assert c.date == "2026-09-20"


def test_schedule_ids_and_bookable_flags():
    classes = parse_schedule(_read("schedule_day.html"), date="2026-09-20")
    assert all(c.class_id for c in classes)
    # at least one class had an open signup form in the fixture
    assert any(c.bookable for c in classes)


def test_discipline_group_tagging():
    classes = parse_schedule(_read("schedule_day.html"), date="2026-09-20")
    names = {c.name: c.discipline_group for c in classes}
    # any thai/kickboxing style class maps to "thai boxing"
    for name, group in names.items():
        if "thai" in name.lower() or "boks" in name.lower():
            assert group == "thai boxing"


def test_parse_bookings():
    bookings = parse_bookings(_read("bookings.html"))
    assert len(bookings) == 1
    b = bookings[0]
    assert b.name == "Thaiboksning"
    assert b.trainer == "Michael Marlow"
    assert b.booking_id == b.class_id


def test_parse_attendance():
    s = parse_attendance(_read("stats.html"))
    assert s.total == 180
    assert s.this_month == 13
    assert s.last_30_days == 18
    assert s.total_hours == pytest.approx(190.5)
    assert s.by_discipline  # per-discipline breakdown present
    assert s.by_discipline.get("Thaiboksning fundamentals") == 74
    assert s.latest_date == "19.09.2026"
    assert s.latest_class == "Thaiboksning"


def test_find_csrf_for():
    html = _read("schedule_day.html")
    res = find_csrf_for(html, "48066")
    assert res is not None
    csrf, start_date = res
    assert len(csrf) == 64
    assert start_date == "2026-09-20"
    assert find_csrf_for(html, "does-not-exist") is None


def test_empty_day_is_not_a_parse_failure():
    empty = '<div class="mu-training__main"><div class="mu-empty">Ingen hold denne dag</div></div>'
    assert parse_schedule(empty, date="2026-01-01") == []


def test_structural_break_raises():
    with pytest.raises(ParseError):
        parse_schedule("<html><body>totally different page</body></html>")


def test_parse_public_week_groups_by_day():
    week = parse_public_week(_read("public_week.html"))
    assert set(week) == {"2026-09-21", "2026-09-22"}
    assert len(week["2026-09-21"]) == 3  # 2 mats: Mat1 (2) + Rig (1)
    assert len(week["2026-09-22"]) == 1


def test_parse_public_week_class_fields():
    week = parse_public_week(_read("public_week.html"))
    bjj = week["2026-09-21"][0]
    assert bjj.name == "Elite BJJ"
    assert bjj.start == "2026-09-21 08:00"
    assert bjj.end == "2026-09-21 09:00"
    assert bjj.trainer == "Shimon Mochizuki"
    assert bjj.location == "Mat 1 Kampsport"
    assert bjj.discipline_group == "bjj"
    # public plan carries no live/bookable data
    assert bjj.source == "schedule"
    assert bjj.bookable is False
    assert bjj.class_id == ""
    assert bjj.spots_available is None


def test_parse_public_week_discipline_tagging():
    week = parse_public_week(_read("public_week.html"))
    by_name = {c.name: c.discipline_group for c in week["2026-09-21"]}
    assert by_name["Thaiboksning Fundamentals"] == "thai boxing"
    assert by_name["WOD and Coffee"] == "wod"


def test_parse_public_week_structural_break_raises():
    with pytest.raises(ParseError):
        parse_public_week("<html><body>a totally different page</body></html>")


def test_resolve_discipline_aliases():
    for q in ["kickboxing", "thai boxing", "thaiboxing", "muay thai", "K1", "kickboksning"]:
        assert config.resolve_discipline(q) == "thai boxing"
    assert config.resolve_discipline("bjj") == "bjj"
    assert config.resolve_discipline("no-gi") == "bjj"
    assert config.resolve_discipline("nonsense-xyz") is None
