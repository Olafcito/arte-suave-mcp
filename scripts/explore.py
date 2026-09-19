"""Visit member pages with saved session, capture AJAX traffic per page."""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from capture import RAW, ROOT, Recorder, dump_page, redact, wait_challenge  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "https://am.artesuave.dk/webshop/Account/index.php"

PAGES = {
    "schedule": "?Show=ShowProfile&action=SignUpforclasses",
    "stats": "?Show=ShowProfile&action=Stats",
    "membership": "?Show=ShowProfile&action=MemberMembership",
    "dash_week": "?Show=ShowProfile&dash_tab=week&dash_type=classes",
    "bookings": "?Show=ShowProfile&action=Bookings",
    "sched_sun": "?Show=ShowProfile&action=SignUpforclasses&StartDate=2026-09-20",
}


def main(names):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="da-DK", storage_state=str(ROOT / "storage_state.json")
        )
        page = context.new_page()
        for name in names:
            path = PAGES[name]
            rec = Recorder(name)
            handler = lambda r, rec=rec: rec.on_response(r)  # noqa: E731
            page.on("response", handler)
            page.goto(BASE + path, wait_until="domcontentloaded")
            wait_challenge(page)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(2)
            dump_page(page, f"{name}_page")
            rec.save()
            page.remove_listener("response", handler)
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1:] or list(PAGES))
