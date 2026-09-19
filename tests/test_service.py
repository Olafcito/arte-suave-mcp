import datetime as dt
from pathlib import Path

from arte_suave_mcp import feedback, service

FIX = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---- source routing ---------------------------------------------------------
def test_use_portal_window(monkeypatch):
    # Wednesday 2026-09-23; this week's window is Wed..Sun.
    monkeypatch.setattr(service, "_today", lambda: dt.date(2026, 9, 23))
    assert service._use_portal(dt.date(2026, 9, 21)) is False  # Mon (past)
    assert service._use_portal(dt.date(2026, 9, 23)) is True   # today
    assert service._use_portal(dt.date(2026, 9, 27)) is True   # Sun (week end)
    assert service._use_portal(dt.date(2026, 9, 28)) is False  # next Mon


def _stub_sources(monkeypatch, today):
    monkeypatch.setattr(service, "_today", lambda: today)
    monkeypatch.setattr(service, "get_public", lambda url: _read("public_week.html"))
    monkeypatch.setattr(service, "_fetch_schedule_day", lambda day: _read("schedule_day.html"))
    service._public_week_cache.clear()
    service._schedule_cache.clear()


def test_past_days_come_from_public_plan(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    res = service.get_schedule(date_from="2026-09-21", date_to="2026-09-21")
    assert res["status"] == "ok"
    assert res["data"], "past day should now return the planned classes"
    assert all(c["source"] == "schedule" for c in res["data"])
    assert "note" in res  # planned-data disclaimer present


def test_this_week_upcoming_comes_from_portal(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    res = service.get_schedule(date_from="2026-09-23", date_to="2026-09-23")
    assert res["status"] == "ok"
    assert res["data"]
    assert all(c["source"] == "portal" for c in res["data"])
    assert "note" not in res  # all live, no disclaimer


def test_mixed_range_merges_both_sources(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    res = service.get_schedule(date_from="2026-09-21", date_to="2026-09-23")
    sources = {c["source"] for c in res["data"]}
    assert sources == {"portal", "schedule"}
    # sorted by (date, start)
    dates = [c["date"] for c in res["data"] if c.get("date")]
    assert dates == sorted(dates)


def test_discipline_filter_across_sources(monkeypatch):
    _stub_sources(monkeypatch, dt.date(2026, 9, 23))
    res = service.get_schedule(discipline="thai", date_from="2026-09-21", date_to="2026-09-21")
    assert res["matched_discipline"] == "thai boxing"
    assert res["data"]
    assert all("thai" in c["name"].lower() or "boks" in c["name"].lower() for c in res["data"])


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


def test_submit_feedback_requires_message():
    res = service.submit_feedback("   ")
    assert res["status"] == "error"
