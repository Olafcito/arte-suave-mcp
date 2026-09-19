# arte-suave-mcp

A private [FastMCP](https://github.com/jlowin/fastmcp) server that lets an AI
assistant (Claude, via a custom connector) read my Arte Suave (Copenhagen) class
data — schedule, my bookings, attendance history — and book/cancel classes.

Runs as a single AWS Lambda behind an API Gateway HTTP API. **$0/mo** at
personal volume (see `docs/INFRA.md`). No browser at runtime: plain HTTP against
the portal, including solving the site's proof-of-work shield in pure Python.

## Tools

| Tool | What it does |
|---|---|
| `get_schedule(discipline?, date_from?, date_to?)` | Classes with name, discipline group, trainer, start/end, location, spots. `discipline` matches loosely — "kickboxing", "muay thai", "K1" all resolve to thai boxing — and the gym's **original** class name is always returned. |
| `get_my_bookings()` | Classes you're signed up for. |
| `get_history(date_from?, date_to?)` | Attendance: this-month / 30-day / all-time counts, hours, latest training, per-discipline breakdown. |
| `book_class(class_id)` / `cancel_booking(booking_id)` | Book / cancel (writes to your account). |
| `health_check()` | Verifies login + each parser, per endpoint. |
| `debug_fetch(target)` | Sanitized raw HTML for a target, so the assistant can adapt if the site changes. |

Example asks: *"What thai boxing classes are on this week?"*, *"When is Michael
teaching?"*, *"How many times have I trained this month?"*

## Resilience by design

- All endpoints, selectors, field names and discipline aliases live in one place
  (`arte_suave_mcp/config.py`) — nothing hard-coded in tool logic.
- Responses parse into pydantic models. On a parse failure the tool never raises
  an opaque error or returns partial garbage: it returns
  `{"status": "parse_failed", step, expected, raw_excerpt}` (size-capped,
  sanitized) so the assistant can read the raw page, answer anyway, and tell you
  what changed. `debug_fetch` exists for the same reason.
- Session cookies are reused across invocations (in-memory + DynamoDB) and we
  re-login transparently on expiry. Requests are rate-limited and the schedule
  is cached — polite to the gym's server.
- Contract tests run against redacted HTML fixtures; one opt-in live smoke test
  hits the real portal.

See `DISCOVERY.md` for how the portal works and `docs/INFRA.md` for the AWS
design and cost review.

## Local development

```bash
uv sync
uv run pytest                 # 13 contract/unit tests
uv run ruff check .

# run the server locally (open, no secret)
LOGIN=... PASSWORD=... uv run arte-suave-mcp     # serves http://localhost:8080/mcp

# live smoke test against the real portal (read-only)
ARTESUAVE_LIVE_SMOKE=1 uv run pytest tests/test_live_smoke.py -v
```

Credentials come from `LOGIN`/`PASSWORD` (env or `.env`) locally, and from SSM
SecureString in Lambda. `.env` is gitignored; never commit it.

## Deploy

Prereqs: AWS CLI + `uv`. No Docker, no SAM CLI needed (Linux wheels are fetched
cross-platform by uv). Uses the `nettoday-admin` profile in eu-north-1 by
default — override with `AWS_PROFILE` / `AWS_REGION`.

```bash
bash infra/put-secrets.sh     # stores login/password + a generated MCP secret in SSM
                              # prints MCP_SECRET=... — save it for the connector
bash infra/deploy.sh          # builds, uploads, deploys the CloudFormation stack
                              # prints the MCP endpoint URL
```

Everything is namespaced `artesuave-mcp-*` and tagged `project=artesuave-mcp`,
fully separate from any other project in the account.

## Users & credentials

The server is **multi-user**: each person uses their own Arte Suave account.

- A user's **bearer token** is self-identifying: `<userid>.<secret>` (e.g.
  `anders.3f9a…`). The server splits off `<userid>`, loads that user's stored
  secret, and constant-time compares it — so it never scans all users and only
  ever logs the id, never the secret.
- Each user's gym login lives in SSM SecureStrings under
  `/artesuave-mcp/users/<userid>/{login,password,token}`, and each gets an
  isolated portal session (`session#<userid>` in DynamoDB). No data crosses
  between users.
- The original single secret (`/artesuave-mcp/mcp-secret`, from `put-secrets.sh`)
  still works and maps to a default account, so an existing connector keeps
  running unchanged.

**Add a user** (yourself or a friend) — run in a real terminal (it prompts for
the gym password, which is never echoed or logged):

```bash
bash infra/add-user.sh anders            # prompts for login + password
bash infra/add-user.sh anders a@ex.com   # or pass the email
# → prints the bearer token:  anders.<secret>   (hand it to that user)
```

## Connect Claude (custom connector)

Endpoint (same for everyone): the `/mcp` URL from the deploy output, e.g.
`https://e7rfsehko9.execute-api.eu-north-1.amazonaws.com/mcp`. Auth is a static
**`Authorization: Bearer <token>`** header — no OAuth. Use your `<userid>.<secret>`
token (or the legacy `MCP_SECRET`). All three clients support this:

**Claude Code (CLI)**
```bash
claude mcp add --transport http --scope user arte-suave \
  https://e7rfsehko9.execute-api.eu-north-1.amazonaws.com/mcp \
  --header "Authorization: Bearer <YOUR_TOKEN>"
claude mcp list            # verify; or /mcp inside a session
claude mcp remove arte-suave
```

**Claude Desktop** — edit `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`), then fully
quit and reopen:
```json
{
  "mcpServers": {
    "arte-suave": {
      "url": "https://e7rfsehko9.execute-api.eu-north-1.amazonaws.com/mcp",
      "headers": { "Authorization": "Bearer <YOUR_TOKEN>" }
    }
  }
}
```

**claude.ai (web)** — Settings → Connectors → **Add custom connector**: enter the
`/mcp` URL, choose **No sign-in**, open **Request headers**, add header
`authorization` = `Bearer <YOUR_TOKEN>` (include the `Bearer ` prefix), mark
required, save. (The Request-headers field is in beta; if you don't see it, use
the CLI or Desktop instead.)

The API is open at the AWS edge; the server enforces the token itself. Treat each
token like a password; rotate a user's with `add-user.sh` (or the legacy secret
with `put-secrets.sh`).

## Layout

```
arte_suave_mcp/
  config.py    endpoints, selectors, discipline aliases (edit here when the site changes)
  models.py    pydantic models + ok/parse_failed/error envelope
  waf.py       simply.com proof-of-work solver
  client.py    httpx session client: WAF clearance, login, reuse, one-retry re-login
  parsers.py   selectolax parsers (isolated)
  service.py   tool logic (framework-agnostic)
  server.py    FastMCP tools + ASGI app + per-user auth gate
  creds.py / session_store.py   per-user SSM creds + identity; memory/file/DynamoDB session
infra/         template.yaml (SAM/CFN), deploy.sh, put-secrets.sh, add-user.sh, run.sh
scripts/       Playwright discovery harness (dev only)
tests/         contract tests + fixtures + opt-in live smoke
```
