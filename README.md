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
There are two ways to connect — the login flow is the one to use.

### Log in with your Arte Suave account (recommended)

The server is its own **OAuth 2.1 provider** (the MCP Authorization spec that
Claude speaks). When someone adds the connector, Claude sends them to a login
page hosted by the server; they enter their **own** Arte Suave email/password,
and Claude silently receives a token. No token to copy, no onboarding step, and
whenever a token expires they just log in again.

- The gym portal is *not* an OAuth provider, so we can't delegate to it — the
  login page collects the credentials, verifies them against the portal, and
  stores them so the server can silently re-login when the portal's short-lived
  cookie expires (a reused cookie alone would decay).
- Credentials are stored as SSM SecureStrings encrypted under a **dedicated KMS
  key whose policy grants decrypt only to the Lambda role** — not even the AWS
  account admin can read them from the console. (Not zero-knowledge: the function
  must decrypt them at login time; see `docs/INFRA.md`.)
- Each user gets an isolated portal session (`session#<userid>` in DynamoDB) and
  a user id derived from a one-way hash of their email — the email never appears
  in a parameter name. No data crosses between users.
- OAuth codes/tokens live in DynamoDB under `oauth:*` keys and auto-expire (TTL).

Nothing to run — it works the moment the connector is added (see below).

### Manual token (advanced / headless)

For a scripted client that can't do a browser login, you can still mint a static
bearer token. Run in a real terminal (it prompts for the gym password, never
echoed or logged):

```bash
bash infra/add-user.sh anders            # prompts for login + password
bash infra/add-user.sh anders a@ex.com   # or pass the email
# → prints the bearer token:  anders.<secret>   (used as Authorization: Bearer)
```

The legacy single secret (`/artesuave-mcp/mcp-secret`, from `put-secrets.sh`)
also still works and maps to a default account, so an existing connector keeps
running unchanged.

## Connect Claude (custom connector)

Endpoint (same for everyone): the `/mcp` URL from the deploy output, e.g.
`https://e7rfsehko9.execute-api.eu-north-1.amazonaws.com/mcp`.

**With login (recommended)** — just add the connector by URL; Claude discovers
the OAuth flow and shows the Arte Suave login page. No headers, no token.

- **claude.ai (web)**: Settings → Connectors → **Add custom connector** → enter
  the `/mcp` URL → **Connect**. The login page opens; sign in with your Arte
  Suave account.
- **Claude Desktop**: Settings → Connectors → **Add custom connector** → the
  `/mcp` URL. It opens the same login page on connect.
- **Claude Code (CLI)**:
  ```bash
  claude mcp add --transport http --scope user arte-suave \
    https://e7rfsehko9.execute-api.eu-north-1.amazonaws.com/mcp
  # first use opens the browser login; then: claude mcp list  (or /mcp in a session)
  ```

**With a static token** (the manual-token/legacy path) — pass it as a header
instead of logging in:

- **Claude Code**: add `--header "Authorization: Bearer <TOKEN>"` to the command
  above.
- **Claude Desktop** — `claude_desktop_config.json` (macOS: `~/Library/Application
  Support/Claude/`, Windows: `%APPDATA%\Claude\`), then quit and reopen:
  ```json
  {
    "mcpServers": {
      "arte-suave": {
        "url": "https://e7rfsehko9.execute-api.eu-north-1.amazonaws.com/mcp",
        "headers": { "Authorization": "Bearer <TOKEN>" }
      }
    }
  }
  ```
- **claude.ai (web)**: Add custom connector → choose **No sign-in** → **Request
  headers** → `authorization` = `Bearer <TOKEN>`.

The API is open at the AWS edge; the server enforces auth itself on every
request. Treat every token like a password.

## Layout

```
arte_suave_mcp/
  config.py    endpoints, selectors, discipline aliases (edit here when the site changes)
  models.py    pydantic models + ok/parse_failed/error envelope
  waf.py       simply.com proof-of-work solver
  client.py    httpx session client: WAF clearance, login, reuse, one-retry re-login
  parsers.py   selectolax parsers (isolated)
  service.py   tool logic (framework-agnostic)
  server.py    FastMCP tools + ASGI app + per-user auth gate (secret / token / OAuth)
  oauth.py     OAuth 2.1 server: discovery, DCR, login page, /authorize + /token (PKCE)
  creds.py / session_store.py   per-user SSM creds + identity; memory/file/DynamoDB session
infra/         template.yaml (SAM/CFN), deploy.sh, put-secrets.sh, add-user.sh, run.sh
scripts/       Playwright discovery harness (dev only)
tests/         contract tests + fixtures + opt-in live smoke
```
