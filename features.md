# Features

Backlog and specs for Arte Suave MCP changes. Feedback from `submit_feedback`
lands here (or gets fixed straight away) when it is reviewed — see the
`handling-feedback` skill. Each entry: status, where it came from, the spec.

Status: `planned` → `in progress` → `shipped (PR #n)`.

---

## F1 — Release detection from the portal, not the calendar

- **Status:** shipped (PR #3)
- **Source:** `feedback#u5272fd209b89cd5d#1789892089` (2026-09-20)

### Problem

`get_schedule` decides which days are bookable from the date alone: today through
this week's Sunday comes from the live portal, everything later comes from the
public weekly plan and is reported as "not released yet". The gym releases next
week's classes on Sunday, so from that moment until Monday the tool calls a
bookable week "unreleased", returns no `class_id`/`bookable`/`signed_up`/spots
for it, and names a release date that is already today. Booking through the MCP
is blocked for that whole window.

### Behaviour

Whether a class can be booked is answered by the site, per class, never inferred
from the calendar.

1. **Past days** — public weekly plan, as today.
2. **Today → this week's Sunday** — portal, as today. An empty portal day is a
   genuinely empty day (no fallback to the plan).
3. **Future weeks** — ask the portal first, week by week in date order:
   - If the portal returns classes for any requested day of that week, the week
     is **released**: all its requested days come from the portal with the normal
     live fields (`class_id`, `bookable`, `signed_up`, spots).
   - If every requested day of the week comes back empty, the week is
     **unreleased**: its days come from the public plan, without booking fields,
     and the top-level `note` says so.
   - Releases are sequential, so once one week is unreleased, later weeks are not
     probed — they go straight to the public plan.
4. **Per-class `bookable`** stays the one flag on every live row (it comes from
   the class's own signup form). Planned rows keep carrying no booking fields —
   one plain top-level note instead of per-row `false` (owner's interface
   principles; earlier feedback asked for exactly this).
5. **Note wording** for unreleased days: "Classes from X onward have not been
   released for booking yet", followed by
   - "; the next batch is expected to open Sunday dd-mm-yyyy." when that Sunday
     is in the future,
   - "; they are expected to open later today." when that Sunday is today,
   - nothing more when that Sunday has already passed.

Known edge: a single requested day that is genuinely empty inside a released
future week is indistinguishable from an unreleased one and is served from the
plan. Accepted — asking for a wider range resolves it.

`book_class` needs no change: it already searches the portal for the class row
across the next `MAX_SCHEDULE_DAYS` days.

### `debug_fetch` excerpt

Same feedback: `debug_fetch("schedule")` returned only the page head/nav because
the 4,000-char excerpt starts at the top of a ~97k-char document. The excerpt
now starts at the training UI region (`config.SEL["main"]`) when the page has
one, and the response carries `raw_offset` so the caller knows where it began.
Pages without that region behave as before.

### Tests

- Sunday after release: next week's days are live rows with `class_id`, no note.
- Midweek, next week unreleased: plan rows + note naming the coming Sunday.
- Release Sunday before the release happened: note says "later today", never a
  date that is already today as if it were in the future.
- Two future weeks requested, first unreleased: second week is never fetched
  from the portal.
- Empty day inside the current week stays empty, no note.
- `debug_fetch` excerpt contains the class region of a long page.

---

## F2 — Upload a training picture (experiment)

- **Status:** shipped (PR #4) — link route verified live 2026-09-20; client test matrix still to run
- **Source:** owner request (2026-09-20): test whether Claude clients can get a
  user's photo out to an MCP tool.

### Background

MCP has no file-transfer primitive yet (SEP-2631 proposes one; no host ships
it). What exists today:

| Route | How | Reality |
|---|---|---|
| Inline base64 argument | model types the bytes into the tool call | The model sees an attached photo as pixels, not bytes, so in claude.ai / mobile it can only invent base64. Works only when the client has the real file (a code sandbox, Claude Code) and the file is small — every byte is an output token. |
| Upload link ("ticket") | tool returns a short-lived URL; bytes travel over plain HTTPS outside MCP | Works on every client incl. mobile: tap link, pick photo. The pattern the MCP draft spec is converging on. |
| Sandbox push | a client code sandbox POSTs the file to the ticket's presigned target | No tokens spent on bytes; depends on the sandbox's network egress allowlist. |
| Host file params | ChatGPT Apps SDK can pass an uploaded file as a download URL | ChatGPT-only; not built here. |

### Behaviour

- `upload_training_pic(image_base64?, note?)`
  - With `image_base64` (raw or `data:` URI): strictly decode, identify the
    format from magic bytes (JPEG/PNG/WebP/GIF/HEIC), cap at 3 MiB, store.
    Invented or truncated base64 fails with a plain message telling the
    assistant to use the link instead — that failure is itself the test result.
  - Without it: returns `upload_url`, valid 15 minutes, for the user to open.
    The page is a single "choose photo" form that posts straight to S3
    (presigned POST, ≤ 15 MiB, `image/*` only) and redirects back to a
    confirmation. `GET <upload_url>?format=json` returns the same presigned POST
    for a code sandbox to use.
- `get_training_pics()` — the caller's pictures, newest first: `uploaded_at`,
  `size`, `type`, `note`, and a 1-hour `view_url`.
- Pictures are private per user: S3 key `pics/<user_id>/<ts>-<id>`, bucket
  blocks all public access, access only through presigned URLs.
- The upload ticket is the only credential on `/upload/<token>`: 32 random
  bytes, stored in the sessions table with a `ttl`, bound to one user and one
  object key. The destination never comes from tool arguments.
- Local dev / tests (no bucket configured): inline uploads are kept in memory;
  the link route reports that uploads aren't configured.

### Infra

`PicsBucket` (private, SSE, retained on stack delete), `ARTESUAVE_PICS_BUCKET`
env var, Lambda role gets Put/Get on `pics/*` and ListBucket on that prefix.

### Test matrix (manual, after deploy)

1. claude.ai web, photo attached, "upload this" → expect the link route (or a
   rejected invented base64).
2. Claude mobile, same.
3. claude.ai with code execution on → sandbox push via `?format=json`, or real
   base64 of a small file.
4. Claude Code with a local file → real base64 (small) works.
