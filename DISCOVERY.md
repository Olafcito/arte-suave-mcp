# Arte Suave portal — how it works

Reference for the portal mechanics the server depends on. Endpoints and
selectors are mirrored in `arte_suave_mcp/config.py`; this file keeps the
reasoning behind them. Originally captured with the Playwright harness in
`scripts/` (raw dumps in gitignored `discovery/raw/`); when the site changes,
the live `debug_fetch` tool is usually enough to re-check. Trimmed, scrubbed
fixtures for tests live in `tests/fixtures/`.

## Platform

- Member portal is a **custom PHP app** ("Academy Management" / Mochizuki
  Group), server-rendered HTML. Bootstrap 5 + Font Awesome front end.
- Host `am.artesuave.dk` sits behind **simply.com's WAF** with a JavaScript
  **proof-of-work challenge** (HTTP 454/455). Any request without prior
  clearance gets an HTML interstitial titled "Checking your browser…".
- The native app `com.artesuave.members` talks to the **same** endpoints; there
  is no separate cleaner JSON API for the data we need. Data comes back as HTML.

## WAF proof-of-work (must clear before anything else)

The interstitial ships a self-contained SHA-256 PoW solver. Parameters are in
the page HTML:

```
var T="<64-hex token>", TS="<unix ts>", D=<difficulty bits, seen: 16>;
```

Solve: find the smallest integer `nonce` such that `sha256(f"{T}:{nonce}")` has
`>= D` **leading zero bits** (bitwise, not hex chars — see `lz()` in the page).
Then:

```
POST https://am.artesuave.dk/.sc-verify/
Content-Type: application/x-www-form-urlencoded
ts=<TS>&nonce=<nonce>&token=<T>
-> {"ok":true,"cookie":"<value>"}   set as cookie  sc_clearance=<value>
```

D=16 averages ~65k SHA-256 hashes, well under 100 ms in Python. Implemented in
`arte_suave_mcp/waf.py`, algorithm unit-tested offline in `tests/test_waf.py`.

**Verified:** the leading-zero-bit function matches the site's `lz()` exactly
(offline). The live `/.sc-verify/` round-trip is exercised only by the opt-in
live smoke test — running it was flagged as "bypassing a security check" by the
local safety classifier, so it is gated behind `ARTESUAVE_LIVE_SMOKE=1` and left
for explicit human opt-in. Clearance appears to bind to the session/IP: once a
`PHPSESSID` is cleared, plain httpx reuses it without re-solving (confirmed —
a browser-obtained session drove httpx through the WAF with no `sc_clearance`).

## Login

```
POST https://am.artesuave.dk/ajax/AjaxJson.php?action=LoginUser
X-Requested-With: fetch
Content-Type: application/json
{"AccountID":"1","email":"<email>","password":"<password>"}
-> {"success":true,...}   sets PHPSESSID session cookie
```

- `AccountID=1` is Arte Suave's club id (hidden field on the login form).
- Session = the `PHPSESSID` cookie. Reused across requests; on expiry the portal
  redirects to the login form, which is the re-login trigger.

## Data endpoints (all GET, HTML, require the session cookie)

Base: `https://am.artesuave.dk/webshop/Account/index.php`

| Purpose        | Query                                                        |
|----------------|--------------------------------------------------------------|
| Schedule (day) | `?Show=ShowProfile&action=SignUpforclasses&StartDate=YYYY-MM-DD` |
| My bookings    | `?Show=ShowProfile&action=Bookings`                          |
| Attendance     | `?Show=ShowProfile&action=Stats`                             |
| Membership     | `?Show=ShowProfile&action=MemberMembership`                  |

Schedule is **one day per request** (a `StartDate` day nav). Next week's plan is
released each Sunday; past days and unreleased weeks come back empty. For
those, `get_schedule` falls back to the **public weekly plan** on the marketing
site: `https://artesuave.dk/traeningstider/?StartDate=DD-MM-YYYY` (a Monday),
no login, same simply.com WAF, an `<h1>` per day, `<h5>` per mat, then a
`w3-table` of Tid / Hold / Instruktører. It carries no spots or booking
forms, so those classes are planned only. Category filter chips exist:
`BJJ GI, BJJ NO-GI, KICKBOXING, KIDS, MMA, OPEN GYM, OTHER, WOD, YOGA`
(filter is server-side via `kategori[]`), but we filter client-side after
parsing so we always keep the gym's original class name.

### Class row shape (schedule + bookings)

```html
<div class="md-class-row md-class-row--has-meta" id="48066">   <!-- id = WorkScheduleID -->
  <div class="md-class-row__time">
    <span class="md-class-row__time-start">09.00</span>
    <span class="md-class-row__time-end">– 10.00</span></div>
  <div class="md-class-row__info">
    <div class="md-class-row__name">Big Boys</div>
    <div class="md-class-row__meta">
      <div class="md-class-row__area"><span>Mat 2  Kampsport</span></div>
      <div class="md-class-row__desc">+90 kg guys only</div></div></div>
  <div class="md-class-row__instructor">
    <span class="md-class-row__instructor-name">Michael Marlow</span></div>
  <div class="md-class-row__spots">
    <span class="md-class-row__spots-count">46 / 46</span>
    <span class="md-class-row__spots-label">ledige pladser</span></div>
  <div class="md-class-row__action">
    <form class="mu-signup-form" method="post" action="?...">
      <input name="csrf" value="<64-hex>">
      <input name="action" value="SignUpforclasses">
      <input name="StartDate" value="2026-09-20">
      <input name="ClassSignupAction" value="signup">   <!-- or "unregister" -->
      <input name="WorkScheduleID" value="48066">
      <button type="submit">Tilmeld</button></form></div>
</div>
```

Notes:
- `spots-count` is **"available / capacity"** (e.g. `46 / 46` = empty class).
- Closed classes show text "Tilmelding er lukket!" and no signup form.
- Instructor cell can be empty.
- Times use Danish `HH.MM`; `time-end` is prefixed with an en dash.

### Attendance (Stats)

Aggregates only: this-month count, last-30-days count, all-time count, total
hours, latest training (date + class name), plus a per-discipline breakdown
embedded in a Chart.js `kLabels`/`kData` array in inline JS. No per-session
history table on this page.

## Booking / cancelling

Same-origin form POST to `?Show=ShowProfile&action=SignUpforclasses&StartDate=…`
with the row's hidden fields, flipping `ClassSignupAction`:
- **book**: `ClassSignupAction=signup`, `WorkScheduleID=<id>`, `csrf=<token>`
- **cancel**: `ClassSignupAction=unregister`, same fields

The `csrf` token is per-page-render and lives on each row's form, so book/cancel
must first GET the day, read the fresh `csrf` for that `WorkScheduleID`, then
POST. The JS `api()` helper posts these and reads `{ok, message}`.

Both `signup` and `unregister` work live (`book_class` / `cancel_booking` are
in daily use from claude.ai and ChatGPT); each confirms by reading the
bookings page back. The opt-in live smoke test stays read-only.

## Auth/session lifetime

- `PHPSESSID`: standard PHP session, no explicit expiry cookie seen; dies
  server-side after inactivity → portal serves the login form again.
- `sc_clearance`: WAF clearance, `max-age=86400` (24 h) per the challenge JS.
- Strategy: persist `{cookies, obtained_at}` in DynamoDB + in-memory cache;
  detect "logged out" (login form present / redirect) and re-login once,
  re-solving the WAF PoW only if challenged.

## Redaction

`scripts/capture.py` replaces credential values, `PHPSESSID`/token-looking
strings on write. Sensitive request headers (cookie/authorization) are stored
only as `<present, N chars>`. Test fixtures are hand-trimmed to structural HTML
with other members' names removed (public instructor names kept — they are the
whole point of the "when does Michael teach" use case).
