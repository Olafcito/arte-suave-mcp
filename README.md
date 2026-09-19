# arte-suave-mcp

Ask your AI assistant *"what thai boxing classes are on this week?"* — and let it
book you in.

This is a small [MCP](https://modelcontextprotocol.io) server that connects
Claude (or any MCP client) to my [Arte Suave](https://artesuave.dk) gym in
Copenhagen. It reads the class schedule, your bookings, and your attendance
history, and it can book and cancel classes for you — all in plain conversation.

It runs as a single AWS Lambda and costs **$0/mo** at personal volume. There's no
headless browser at runtime: it talks to the gym portal over plain HTTP and
solves the site's proof-of-work shield in pure Python.

> [!NOTE]
> Arte Suave teaches BJJ, MMA, thai boxing, and wrestling. This project isn't
> affiliated with the gym — it's a personal tool built against its public portal.

## What you can ask

Once connected, you talk to your assistant normally:

- *"What thai boxing classes are on this week?"*
- *"Book me into the 17:00 class tomorrow."*
- *"What was on last week?"* / *"What's the muay thai schedule next week?"*
- *"How many times have I trained this month?"*
- *"Cancel my Friday booking."*

Disciplines match loosely — "kickboxing", "muay thai", and "K1" all resolve to
thai boxing — and the assistant always gets the gym's original class name back.

## The tools

| Tool | What it does |
|---|---|
| `get_schedule(discipline?, date_from?, date_to?)` | Classes with name, discipline, trainer, time, location, and spots. **Live** data (real spots, bookable) for this week's upcoming days; the **planned** weekly schedule (any past/future week, no spots) for everything else. Each class is tagged `source` = `portal` or `schedule`. |
| `get_my_bookings()` | Classes you're signed up for. |
| `get_history(date_from?, date_to?)` | Attendance: this-month / 30-day / all-time counts, hours, latest training, per-discipline breakdown. |
| `book_class(class_id)` / `cancel_booking(booking_id)` | Book / cancel on your account. Each **self-confirms** by reading your bookings back, and errors out if the change didn't land. |
| `submit_feedback(message, context?)` | Records feedback for the owner to review. |
| `health_check()` | Verifies login and every parser, per endpoint. |
| `debug_fetch(target)` | Sanitized raw HTML for a target, so the assistant can adapt if the site changes. |

> [!TIP]
> The schedule spans past and future because it reads **two sources**. The member
> portal only serves the current week's upcoming days (with live spots and
> booking). Everything else — days already past, and future weeks — comes from
> the gym's public weekly schedule, which is the *plan* and may still change.

## Connect Claude

The endpoint is the same for everyone: the `/mcp` URL from the deploy output,
e.g. `https://e7rfsehko9.execute-api.eu-north-1.amazonaws.com/mcp`.

### Log in with your gym account (recommended)

The server is its own **OAuth 2.1 provider** (the MCP Authorization flow Claude
speaks). Add the connector by URL and Claude sends you to a login page hosted by
the server; sign in with your **own** Arte Suave email and password, and Claude
receives a token silently. Nothing to copy, and when a token expires you just log
in again.

- **claude.ai (web)** — Settings → Connectors → **Add custom connector** → paste
  the `/mcp` URL → **Connect**. The login page opens.
- **Claude Desktop** — Settings → Connectors → **Add custom connector** → the
  `/mcp` URL. The same login page opens on connect.
- **Claude Code (CLI)**:
  ```bash
  claude mcp add --transport http --scope user arte-suave \
    https://e7rfsehko9.execute-api.eu-north-1.amazonaws.com/mcp
  # first use opens the browser login; then check with: claude mcp list
  ```

### Static token (advanced / headless)

For a scripted client that can't do a browser login, mint a static bearer token
(see [Users & credentials](#users--credentials)) and pass it as a header:

- **Claude Code** — add `--header "Authorization: Bearer <TOKEN>"` to the command above.
- **Claude Desktop** — edit `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`), then restart:
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
- **claude.ai (web)** — Add custom connector → **No sign-in** → **Request headers** → `authorization` = `Bearer <TOKEN>`.

> [!WARNING]
> The endpoint is open at the AWS edge; the server enforces auth itself on every
> request. Treat every token like a password.

## Users & credentials

The server is **multi-user** — each person uses their own Arte Suave account, and
no data crosses between users.

- Each user gets an isolated portal session (`session#<userid>` in DynamoDB) and a
  user id derived from a one-way hash of their email — the email never appears in
  a parameter name.
- Credentials are stored as SSM SecureStrings encrypted under a **dedicated KMS
  key whose policy grants decrypt only to the Lambda role** — not even the AWS
  account admin can read them from the console.
- The gym portal isn't an OAuth provider, so the login page collects the
  credentials, verifies them against the portal, and stores them so the server
  can silently re-login when the portal's short-lived cookie expires.
- OAuth codes and tokens live in DynamoDB under `oauth:*` keys and auto-expire (TTL); tokens are stored hashed.

To mint a static token for a headless client, run in a real terminal (it prompts
for the gym password, never echoed or logged):

```bash
bash infra/add-user.sh anders            # prompts for login + password
bash infra/add-user.sh anders a@ex.com   # or pass the email
# → prints the bearer token:  anders.<secret>
```

The legacy single secret (`/artesuave-mcp/mcp-secret`, from `put-secrets.sh`) also
still works and maps to a default account, so an existing connector keeps running.

## How it's built to last

The gym portal is an undocumented site that can change under us. The design
assumes that:

- Every endpoint, selector, field name, and discipline alias lives in one place
  (`arte_suave_mcp/config.py`) — nothing is hard-coded in the tool logic.
- Responses parse into pydantic models. On a parse failure a tool never raises an
  opaque error or returns partial garbage — it returns
  `{"status": "parse_failed", step, expected, raw_excerpt}` (size-capped and
  **sanitized**) so the assistant can read the raw page, answer anyway, and tell
  you what changed. `debug_fetch` exists for the same reason.
- Session cookies are reused across invocations (in-memory + DynamoDB) with a
  transparent re-login on expiry. Requests are rate-limited and the schedule is
  cached — polite to the gym's server.
- Contract tests run against redacted HTML fixtures; one opt-in live smoke test
  hits the real portal.

See [`DISCOVERY.md`](DISCOVERY.md) for how the portal actually works.

## Local development

```bash
uv sync
uv run pytest                 # contract/unit tests
uv run ruff check .

# run the server locally (open, no secret)
LOGIN=... PASSWORD=... uv run arte-suave-mcp     # serves http://localhost:8080/mcp

# live smoke test against the real portal (read-only)
ARTESUAVE_LIVE_SMOKE=1 uv run pytest tests/test_live_smoke.py -v
```

Credentials come from `LOGIN`/`PASSWORD` (env or `.env`) locally, and from SSM
SecureString in Lambda.

> [!IMPORTANT]
> `.env` is gitignored and must never be committed. It holds real gym
> credentials.

## Deploy

Prereqs: AWS CLI and `uv`. No Docker or SAM CLI needed — Linux wheels are fetched
cross-platform by uv. Defaults to the `nettoday-admin` profile in `eu-north-1`;
override with `AWS_PROFILE` / `AWS_REGION`.

```bash
bash infra/put-secrets.sh     # stores login/password + a generated MCP secret in SSM
                              # prints MCP_SECRET=… — save it for the connector
bash infra/deploy.sh          # builds, uploads, deploys the CloudFormation stack
                              # prints the MCP endpoint URL
```

Everything is namespaced `artesuave-mcp-*` and tagged `project=artesuave-mcp`,
fully separate from anything else in the account. Pushing to `main` deploys
automatically via GitHub Actions (OIDC — no stored AWS keys).

## Layout

```
arte_suave_mcp/
  config.py    endpoints, selectors, discipline aliases (edit here when the site changes)
  models.py    pydantic models + ok/parse_failed/error envelope + HTML sanitizer
  waf.py       simply.com proof-of-work solver
  client.py    httpx session client: WAF clearance, login, reuse, one-retry re-login;
               plus get_public() for the no-auth public weekly schedule
  parsers.py   selectolax parsers (isolated): portal pages + public weekly schedule
  service.py   tool logic (framework-agnostic); routes schedule days portal-vs-public
  feedback.py  stores submit_feedback notes (DynamoDB; in-memory fallback locally)
  server.py    FastMCP tools + ASGI app + per-user auth gate (secret / token / OAuth)
  oauth.py     OAuth 2.1 server: discovery, DCR, login page, /authorize + /token (PKCE)
  creds.py / session_store.py   per-user SSM creds + identity; memory/file/DynamoDB session
infra/         template.yaml (SAM/CFN), deploy.sh, put-secrets.sh, add-user.sh, run.sh
scripts/       Playwright discovery harness (dev only)
tests/         contract tests + fixtures + opt-in live smoke
```
