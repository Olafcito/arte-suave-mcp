import datetime as dt
from pathlib import Path

from arte_suave_mcp import feedback, service

FIX = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---- source routing ---------------------------------------------------------
# What the portal serves for a day it has no classes for (unreleased or empty).
EMPTY_DAY = '<div class="mu-training__main"><p class="mu-empty">Ingen hold denne dag</p></div>'


def _stub_sources(monkeypatch, today, released_until=None, empty_days=()):
    """Portal serves classes through `released_until` (default: this week's
    Sunday) and an empty day beyond it. Returns the list of days fetched."""
    if released_until is None:
        released_until = today + dt.timedelta(days=6 - today.weekday())
    fetched: list[str] = []

    def fetch_day(day):
        fetched.append(day)
        if day in empty_days or dt.date.fromisoformat(day) > released_until:
            return EMPTY_DAY
        return _read("schedule_day.html")

    monkeypatch.setattr(service, "_today", lambda: today)
    monkeypatch.setattr(service, "get_public", lambda url: _read("public_week.html"))
    monkeypatch.setattr(service, "_fetch_schedule_day", fetch_day)
    monkeypatch.setattr(service, "_booked_ids", lambda: set())
    service._public_week_cache.clear()
    service._schedule_cache.clear()
    return fetched


def test_past_days_come_from_public_plan(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    res = service.get_schedule(date_from="2026-09-21", date_to="2026-09-21")
    assert res["status"] == "ok"
    assert res["data"], "past day should now return the planned classes"
    assert all("class_id" not in c for c in res["data"])  # plan rows carry no booking data
    assert "note" in res  # provenance note present


def test_this_week_upcoming_comes_from_portal(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    res = service.get_schedule(date_from="2026-09-23", date_to="2026-09-23")
    assert res["status"] == "ok"
    assert res["data"]
    assert all(c["class_id"] for c in res["data"])
    assert "note" not in res  # all live, no disclaimer


def test_mixed_range_merges_both_sources(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    res = service.get_schedule(date_from="2026-09-21", date_to="2026-09-23")
    live = [c for c in res["data"] if "class_id" in c]
    planned = [c for c in res["data"] if "class_id" not in c]
    assert live and planned
    # sorted by (date, start)
    dates = [c["date"] for c in res["data"] if c.get("date")]
    assert dates == sorted(dates)


# ---- feedback: signed_up flag + a simple, non-technical response shape ------
def test_portal_rows_flag_signed_up_from_bookings(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    monkeypatch.setattr(service, "_booked_ids", lambda: {"48067"})
    res = service.get_schedule(date_from="2026-09-23", date_to="2026-09-23")
    by_id = {c["class_id"]: c for c in res["data"]}
    assert by_id["48067"]["signed_up"] is True
    assert all(c["signed_up"] is False for c in res["data"] if c["class_id"] != "48067")


def test_bookings_cross_check_failure_degrades_gracefully(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))

    def boom():
        raise service.AuthError("bookings unavailable")

    monkeypatch.setattr(service, "_booked_ids", boom)
    res = service.get_schedule(date_from="2026-09-23", date_to="2026-09-23")
    assert res["status"] == "ok"
    assert all(c["signed_up"] is False for c in res["data"])


def test_portal_rows_hide_internal_fields(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    res = service.get_schedule(date_from="2026-09-23", date_to="2026-09-23")
    for c in res["data"]:
        assert "source" not in c
        assert "booking_open" not in c  # open is the default; only 'closed' is news
        assert None not in c.values()


def test_planned_rows_hide_booking_fields(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    res = service.get_schedule(date_from="2026-09-21", date_to="2026-09-21")
    assert res["data"]
    hidden = ("class_id", "bookable", "booking_open", "signed_up",
              "spots_available", "capacity", "source")
    for c in res["data"]:
        for key in hidden:
            assert key not in c
        assert None not in c.values()
    assert "public weekly plan" in res["note"]


def test_unreleased_future_days_get_release_note(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    res = service.get_schedule(date_from="2026-09-28", date_to="2026-09-29")
    assert res["status"] == "ok"
    assert all("class_id" not in c for c in res["data"])
    assert "2026-09-28" in res["note"]  # first unreleased day named plainly
    assert "27-09-2026" in res["note"]  # expected release: the Sunday before that week


# ---- feedback: release is what the portal shows, not what the calendar says --
def test_released_next_week_is_live(monkeypatch):
    # Sunday, and the gym has already released next week.
    _stub_sources(monkeypatch, dt.date(2026, 9, 20), released_until=dt.date(2026, 9, 27))
    res = service.get_schedule(date_from="2026-09-21", date_to="2026-09-27")
    assert res["status"] == "ok"
    assert res["data"]
    assert all(c["class_id"] for c in res["data"])
    assert all("bookable" in c and "signed_up" in c for c in res["data"])
    assert "note" not in res


def test_release_day_before_release_says_later_today(monkeypatch):
    # Sunday morning: next week should open today but the portal is still empty.
    _stub_sources(monkeypatch, dt.date(2026, 9, 20))
    res = service.get_schedule(date_from="2026-09-21", date_to="2026-09-22")
    assert "2026-09-21" in res["note"]
    assert "later today" in res["note"]
    assert "20-09-2026" not in res["note"]


def test_weeks_after_an_unreleased_week_are_not_probed(monkeypatch):
    fetched = _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    res = service.get_schedule(date_from="2026-09-28", date_to="2026-10-06")
    assert res["status"] == "ok"
    assert fetched, "the first future week is probed on the portal"
    assert all(d < "2026-10-05" for d in fetched)


def test_empty_day_this_week_stays_empty(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23), empty_days=("2026-09-24",))
    res = service.get_schedule(date_from="2026-09-24", date_to="2026-09-24")
    assert res["status"] == "ok"
    assert res["data"] == []
    assert "note" not in res


def test_discipline_filter_across_sources(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    res = service.get_schedule(discipline="thai", date_from="2026-09-21", date_to="2026-09-21")
    assert res["matched_discipline"] == "thai boxing"
    assert res["data"]
    assert all("thai" in c["name"].lower() or "boks" in c["name"].lower() for c in res["data"])


def test_bookings_hide_internal_fields(monkeypatch):
    class FakeClient:
        def get_authed(self, url):
            return _read("bookings.html")

    monkeypatch.setattr(service, "get_client", lambda: FakeClient())
    monkeypatch.setattr(service, "_today", lambda: dt.date(2026, 9, 19))
    res = service.get_my_bookings()
    assert res["status"] == "ok"
    assert res["data"]
    for b in res["data"]:
        assert b["booking_id"]
        for key in ("source", "booking_open", "bookable"):
            assert key not in b
        assert None not in b.values()


# ---- book / cancel confirmation --------------------------------------------
def test_confirm_book_success(monkeypatch):
    monkeypatch.setattr(service, "_booked_ids", lambda: {"48066"})
    res = service._confirm_signup("48066", expect_booked=True, step="book_class")
    assert res["status"] == "ok"
    assert res["data"]["confirmed"] is True
    assert res["data"]["booked"] is True


def test_confirm_book_not_reflected_is_error(monkeypatch):
    monkeypatch.setattr(service, "_booked_ids", lambda: set())
    res = service._confirm_signup("48066", expect_booked=True, step="book_class")
    assert res["status"] == "error"
    assert "did not appear" in res["message"]


def test_confirm_cancel_still_present_is_error(monkeypatch):
    monkeypatch.setattr(service, "_booked_ids", lambda: {"48066"})
    res = service._confirm_signup("48066", expect_booked=False, step="cancel_booking")
    assert res["status"] == "error"
    assert "still in your bookings" in res["message"]


def test_confirm_cancel_success(monkeypatch):
    monkeypatch.setattr(service, "_booked_ids", lambda: set())
    res = service._confirm_signup("48066", expect_booked=False, step="cancel_booking")
    assert res["status"] == "ok"
    assert res["data"]["booked"] is False


# ---- submit_feedback --------------------------------------------------------
def test_submit_feedback_stores(monkeypatch):
    monkeypatch.delenv("ARTESUAVE_SESSION_TABLE", raising=False)
    feedback._memory.clear()
    res = service.submit_feedback("the schedule answer was wrong", context="asked about next week")
    assert res["status"] == "ok"
    assert res["data"]["id"].startswith("feedback#")
    assert len(feedback._memory) == 1
    assert feedback._memory[0]["message"] == "the schedule answer was wrong"
    assert feedback._memory[0]["context"] == "asked about next week"
    assert feedback._memory[0]["handled"] is False  # new feedback starts open


def test_debug_fetch_excerpt_starts_at_training_region(monkeypatch):
    page = "<html><head>" + "x" * 20000 + '</head><div class="mu-training__main">ROWS</div>'

    class FakeClient:
        def get_authed(self, url):
            return page

    monkeypatch.setattr(service, "get_client", lambda: FakeClient())
    res = service.debug_fetch("schedule")
    assert res["status"] == "ok"
    assert "ROWS" in res["raw_excerpt"]
    assert res["raw_offset"] > 0


def test_submit_feedback_requires_message():
    res = service.submit_feedback("   ")
    assert res["status"] == "error"
