"""Typed models and the structured result envelope.

Every tool returns a dict built from these. On a parse failure we never raise an
opaque error and never return partial garbage: we return a ParseFailure with the
failing step, what was expected, and a size-capped sanitized raw excerpt so the
calling harness can read it and adapt.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import RAW_EXCERPT_MAX


class ClassInfo(BaseModel):
    class_id: str = Field(description="WorkScheduleID; use with book_class")
    name: str = Field(description="the gym's original class name, verbatim")
    discipline_group: str | None = Field(
        default=None, description="canonical group this class matched, if any"
    )
    trainer: str | None = None
    start: str | None = Field(default=None, description="ISO-ish local start, YYYY-MM-DD HH:MM")
    end: str | None = None
    date: str | None = Field(default=None, description="YYYY-MM-DD")
    location: str | None = None
    description: str | None = None
    spots_available: int | None = None
    capacity: int | None = None
    bookable: bool = Field(default=False, description="a signup form was present")
    booking_open: bool = True
    source: str = Field(
        default="portal",
        description=(
            "'portal' = live member data (real spots, bookable this week); "
            "'schedule' = the public weekly plan (any week, no live spots, "
            "not bookable, subject to change)"
        ),
    )


class Booking(ClassInfo):
    booking_id: str | None = Field(
        default=None, description="id to pass to cancel_booking (== class_id here)"
    )
    signed_up: bool = True


class AttendanceSummary(BaseModel):
    this_month: int | None = None
    last_30_days: int | None = None
    total: int | None = None
    total_hours: float | None = None
    latest_date: str | None = None
    latest_class: str | None = None
    by_discipline: dict[str, int] = Field(default_factory=dict)


def ok(data: Any, **extra: Any) -> dict:
    out = {"status": "ok", "data": data}
    out.update(extra)
    return out


def parse_failed(
    step: str, expected: str, raw: str, *, detail: str | None = None
) -> dict:
    """Structured non-fatal failure the harness can act on."""
    excerpt = (raw or "")[:RAW_EXCERPT_MAX]
    return {
        "status": "parse_failed",
        "step": step,
        "expected": expected,
        "detail": detail,
        "raw_excerpt": excerpt,
        "raw_truncated": len(raw or "") > RAW_EXCERPT_MAX,
    }


def error(step: str, message: str, *, raw: str | None = None) -> dict:
    out: dict[str, Any] = {"status": "error", "step": step, "message": message}
    if raw is not None:
        out["raw_excerpt"] = raw[:RAW_EXCERPT_MAX]
    return out


StatusLiteral = Literal["ok", "parse_failed", "error"]
