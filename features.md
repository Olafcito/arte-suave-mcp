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
