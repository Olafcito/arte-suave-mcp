"""Opt-in live smoke test. Hits the real portal; requires credentials.

Enable with:  ARTESUAVE_LIVE_SMOKE=1 uv run pytest tests/test_live_smoke.py -v
Loads .env if present. Read-only: never books or cancels.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ARTESUAVE_LIVE_SMOKE") != "1",
    reason="set ARTESUAVE_LIVE_SMOKE=1 to run live",
)


@pytest.fixture(scope="module", autouse=True)
def _load_env():
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def test_live_health_check():
    from arte_suave_mcp import service

    result = service.health_check()
    assert result["status"] == "ok"
    assert result["data"]["auth"]["status"] == "ok", result


def test_live_schedule_today():
    from arte_suave_mcp import service

    result = service.get_schedule()
    assert result["status"] == "ok", result
    # today may legitimately be empty, but the call must succeed
    assert isinstance(result["data"], list)


def test_live_bookings_and_history():
    from arte_suave_mcp import service

    b = service.get_my_bookings()
    assert b["status"] == "ok", b
    h = service.get_history()
    assert h["status"] == "ok", h
    assert h["data"].get("total") is not None
