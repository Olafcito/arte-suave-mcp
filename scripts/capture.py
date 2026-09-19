"""Discovery harness: drive the Arte Suave member portal with Playwright,
capture network traffic to discovery/raw/ with credentials redacted on write.

Usage: uv run python scripts/capture.py <flow>
Flows: landing, login, explore
Never prints or stores credential values, cookie values or tokens.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "discovery" / "raw"
RAW.mkdir(parents=True, exist_ok=True)
load_dotenv(ROOT / ".env")

LOGIN = os.environ.get("LOGIN", "")
PASSWORD = os.environ.get("PASSWORD", "")

PORTAL = "https://am.artesuave.dk/a/artesuave/webshop"

SENSITIVE_HEADERS = {"cookie", "set-cookie", "authorization", "x-csrf-token"}


def redact(text: str) -> str:
    if not text:
        return text
    for secret in (LOGIN, PASSWORD):
        if secret and len(secret) > 2:
            text = text.replace(secret, "***REDACTED***")
    # redact things that look like session ids / tokens in bodies
    text = re.sub(r"(PHPSESSID=)[^;&\s\"']+", r"\1***", text)
    text = re.sub(r"((?:token|session|sid|auth)[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9+/_.\-]{16,}", r"\1***", text, flags=re.I)
    return text


def header_summary(headers: dict) -> dict:
    out = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in SENSITIVE_HEADERS:
            out[k] = f"<present, {len(v)} chars>"
        else:
            out[k] = redact(v)
    return out


class Recorder:
    def __init__(self, tag: str):
        self.tag = tag
        self.entries = []
        self.counter = 0

    def on_response(self, response):
        req = response.request
        url = response.url
        # skip static assets
        if re.search(r"\.(png|jpe?g|gif|svg|woff2?|ttf|css|ico|webp|mp4)(\?|$)", url):
            return
        entry = {
            "n": self.counter,
            "method": req.method,
            "url": redact(url),
            "status": response.status,
            "resource_type": req.resource_type,
            "content_type": response.headers.get("content-type", ""),
            "req_headers": header_summary(req.headers),
        }
        post = req.post_data
        if post:
            entry["post_data"] = redact(post[:3000])
        ct = entry["content_type"]
        try:
            if "json" in ct:
                body = response.text()
                fn = f"{self.tag}_{self.counter:03d}.json"
                (RAW / fn).write_text(redact(body), encoding="utf-8")
                entry["body_file"] = fn
                entry["body_size"] = len(body)
            elif "html" in ct and req.resource_type in ("document", "xhr", "fetch"):
                body = response.text()
                fn = f"{self.tag}_{self.counter:03d}.html"
                (RAW / fn).write_text(redact(body), encoding="utf-8")
                entry["body_file"] = fn
                entry["body_size"] = len(body)
        except Exception as e:  # body may be unavailable after nav
            entry["body_error"] = str(e)[:100]
        self.entries.append(entry)
        self.counter += 1

    def save(self):
        (RAW / f"{self.tag}_log.json").write_text(
            json.dumps(self.entries, indent=1), encoding="utf-8"
        )
        print(f"captured {len(self.entries)} entries -> {self.tag}_log.json")


def wait_challenge(page):
    """Wait for the simply.com WAF JS challenge to clear."""
    for _ in range(30):
        title = page.title()
        if "Checking your browser" not in title and "Security Incident" not in title:
            return True
        time.sleep(1)
    return False


def dump_page(page, name):
    (RAW / f"{name}.html").write_text(redact(page.content()), encoding="utf-8")
    page.screenshot(path=str(RAW / f"{name}.png"), full_page=True)
    print(f"page '{name}': title={page.title()!r} url={redact(page.url)}")
    # list links and forms for orientation
    links = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => ({href: e.getAttribute('href'), text: (e.innerText||'').trim().slice(0,60)}))"
    )
    forms = page.eval_on_selector_all(
        "form",
        "els => els.map(f => ({action: f.getAttribute('action'), method: f.method, inputs: [...f.querySelectorAll('input,select,button')].map(i => ({tag:i.tagName, type:i.type, name:i.name, id:i.id}))}))",
    )
    (RAW / f"{name}_structure.json").write_text(
        redact(json.dumps({"links": links, "forms": forms}, indent=1)), encoding="utf-8"
    )


def run(flow: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        state_file = ROOT / "storage_state.json"
        ctx_args = {"locale": "da-DK"}
        if state_file.exists() and flow != "landing":
            ctx_args["storage_state"] = str(state_file)
        context = browser.new_context(**ctx_args)
        page = context.new_page()
        rec = Recorder(flow)
        page.on("response", rec.on_response)

        page.goto(PORTAL, wait_until="domcontentloaded")
        if not wait_challenge(page):
            print("WAF challenge did not clear")
            dump_page(page, f"{flow}_challenge")
            return
        page.wait_for_load_state("networkidle")
        dump_page(page, f"{flow}_portal")

        if flow == "login":
            do_login(page)
            page.wait_for_load_state("networkidle")
            dump_page(page, "after_login")
            context.storage_state(path=str(state_file))
            print("storage state saved (gitignored)")

        rec.save()
        browser.close()


def do_login(page):
    # find login entry; try common selectors, else report
    candidates = [
        "text=Log ind", "text=Login", "text=Log på", "a[href*='login' i]",
        "input[type='password']",
    ]
    if page.locator("input[type='password']").count() == 0:
        for sel in candidates[:-1]:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click()
                page.wait_for_load_state("networkidle")
                break
    pw = page.locator("input[type='password']")
    if pw.count() == 0:
        print("NO password field found; see structure dump")
        return
    # the username field: input before password, prefer email/text types
    user_sel = None
    for sel in ["input[type='email']", "input[name*='user' i]", "input[name*='mail' i]", "input[name*='login' i]", "input[type='text']"]:
        if page.locator(sel).count() > 0:
            user_sel = sel
            break
    if not user_sel:
        print("NO username field found")
        return
    page.locator(user_sel).first.fill(LOGIN)
    pw.first.fill(PASSWORD)
    # submit
    for sel in ["button[type='submit']", "input[type='submit']", "form button"]:
        if page.locator(sel).count() > 0:
            page.locator(sel).first.click()
            break
    else:
        pw.first.press("Enter")
    page.wait_for_timeout(3000)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "landing")
