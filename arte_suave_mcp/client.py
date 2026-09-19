"""Authenticated HTTP client for the Arte Suave portal.

Responsibilities:
- clear the simply.com WAF proof-of-work when challenged,
- log in with credentials and reuse the session across invocations,
- transparently re-login once on session expiry,
- rate-limit to be polite.

No browser at runtime. Credentials come from the injected `creds` provider so
they can be sourced from env locally and SSM in Lambda without this module
knowing which.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import httpx

from . import config, waf
from .session_store import SessionStore

Creds = Callable[[], tuple[str, str]]  # returns (email, password)


class AuthError(RuntimeError):
    pass


class WAFError(RuntimeError):
    pass


_public_client: httpx.Client | None = None


def _clear_challenge(
    client: httpx.Client, html: str, verify_url: str, cookie_domain: str, *, what: str
) -> None:
    """Solve the simply.com PoW challenge in `html` and set the clearance cookie
    on `client`. Shared by the portal and public-site clients — they differ only
    in verify URL, cookie domain, and the phrasing of the no-params error."""
    params = waf.parse_challenge(html)
    if not params:
        raise WAFError(f"{what} had no solvable PoW parameters")
    token, ts, difficulty = params
    nonce = waf.solve(token, difficulty)
    resp = client.post(verify_url, data={"ts": ts, "nonce": str(nonce), "token": token})
    try:
        body = resp.json()
    except ValueError as exc:
        raise WAFError(f"unexpected /.sc-verify/ response: {resp.status_code}") from exc
    if not body.get("ok"):
        raise WAFError(f"WAF rejected PoW: {body.get('error', 'unknown')}")
    client.cookies.set(config.WAF_CLEARANCE_COOKIE, body["cookie"], domain=cookie_domain)


def get_public(url: str) -> str:
    """GET a page on the public marketing site (no login).

    Uses one shared client with real browser headers — the WAF hard-blocks a
    bare user-agent but lets a normal browser request straight through. If it
    ever does serve the solvable proof-of-work challenge, we clear it the same
    way the portal client does and retry once.
    """
    global _public_client
    if _public_client is None:
        _public_client = httpx.Client(
            headers=config.DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=config.REQUEST_TIMEOUT,
        )
    resp = _public_client.get(url)
    if waf.is_challenge(resp.text):
        _clear_challenge(
            _public_client,
            resp.text,
            config.PUBLIC_WAF_VERIFY_URL,
            config.PUBLIC_WAF_COOKIE_DOMAIN,
            what="public challenge page",
        )
        resp = _public_client.get(url)
    if resp.status_code >= 400:
        raise WAFError(f"public schedule fetch failed: HTTP {resp.status_code}")
    return resp.text


class PortalClient:
    def __init__(self, creds: Creds, store: SessionStore | None = None) -> None:
        self._creds = creds
        self.store = store or SessionStore()
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._client = httpx.Client(
            headers=config.DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=config.REQUEST_TIMEOUT,
        )
        for name, value in self.store.get_cookies().items():
            self._client.cookies.set(name, value, domain="am.artesuave.dk")

    # -- low-level ------------------------------------------------------------
    def _throttle(self) -> None:
        delta = time.monotonic() - self._last_request
        if delta < config.MIN_REQUEST_INTERVAL:
            time.sleep(config.MIN_REQUEST_INTERVAL - delta)
        self._last_request = time.monotonic()

    def _persist_cookies(self) -> None:
        self.store.put_cookies({c.name: c.value for c in self._client.cookies.jar})

    def _clear_waf(self, html: str) -> None:
        _clear_challenge(
            self._client,
            html,
            config.WAF_VERIFY_URL,
            "am.artesuave.dk",
            what="challenge page",
        )

    def _raw_get(self, url: str) -> httpx.Response:
        self._throttle()
        resp = self._client.get(url)
        if waf.is_challenge(resp.text):
            self._clear_waf(resp.text)
            self._throttle()
            resp = self._client.get(url)
        return resp

    # -- auth -----------------------------------------------------------------
    def _login(self) -> None:
        # ensure WAF cleared via the entry page first
        entry = self._raw_get(config.PORTAL_ENTRY)
        if waf.is_challenge(entry.text):
            self._clear_waf(entry.text)
        email, password = self._creds()
        self._throttle()
        resp = self._client.post(
            config.LOGIN_URL,
            json={"AccountID": config.ACCOUNT_ID, "email": email, "password": password},
            headers={"X-Requested-With": "fetch", "Referer": config.PORTAL_ENTRY + "/"},
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not body.get("success"):
            raise AuthError(f"login failed: {body.get('message', resp.status_code)}")
        self._persist_cookies()

    @staticmethod
    def _looks_logged_out(html: str) -> bool:
        return any(m in html for m in config.LOGGED_OUT_MARKERS) and not any(
            m in html for m in ("member-nav", "Log ud")
        )

    def get_authed(self, url: str) -> str:
        """GET a member page, logging in / re-logging in as needed (one retry)."""
        with self._lock:
            resp = self._raw_get(url)
            if self._looks_logged_out(resp.text):
                self._login()
                resp = self._raw_get(url)
            if self._looks_logged_out(resp.text):
                raise AuthError("still logged out after re-login")
            self._persist_cookies()
            return resp.text

    def post_authed(self, url: str, data: dict) -> httpx.Response:
        """POST a form to a member endpoint with the session (login if needed)."""
        with self._lock:
            if not self.store.get_cookies():
                self._login()
            self._throttle()
            resp = self._client.post(
                url, data=data, headers={"X-Requested-With": "fetch"}
            )
            if resp.status_code in (401, 403) or self._looks_logged_out(resp.text):
                self._login()
                self._throttle()
                resp = self._client.post(
                    url, data=data, headers={"X-Requested-With": "fetch"}
                )
            self._persist_cookies()
            return resp

    def close(self) -> None:
        self._client.close()
