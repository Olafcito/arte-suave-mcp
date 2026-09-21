# arte-suave-mcp

MCP server for the [Arte Suave](https://artesuave.dk) gym portal (Copenhagen).
Reads the class schedule, bookings and attendance history, and books/cancels
classes. Runs as a single AWS Lambda. Not affiliated with the gym.

## Tools

- `get_schedule(discipline?, date_from?, date_to?)` — classes with trainer,
  time, location and spots. Days the gym has released are live and bookable;
  other days come from the gym's public weekly plan.
- `get_my_bookings()` — current bookings.
- `get_history(date_from?, date_to?)` — attendance counts, hours, per-discipline
  breakdown.
- `book_class(class_id)` / `cancel_booking(booking_id)` — write to your account;
  each confirms by reading bookings back.
- `upload_training_pic(image_base64?, note?)` — stores a small picture passed
  as real base64, or returns a 15-minute upload link for the user to open.
  `get_training_pics()` lists them. An experiment; see `features.md` F2.
- `submit_feedback(message, context?)` — leaves a note for the owner.
- `health_check()` — login + parser checks.
- `debug_fetch(target)` — sanitized raw HTML for when the site changes.

Discipline names match loosely: "kickboxing", "muay thai" and "K1" all resolve
to thai boxing.

## Connect

Endpoint: `https://e7rfsehko9.execute-api.eu-north-1.amazonaws.com/mcp`

Add it as a custom connector (claude.ai / Claude Desktop: Settings → Connectors
→ Add custom connector) and sign in with your own Arte Suave account on the
login page that opens.

Claude Code:

```bash
claude mcp add --transport http --scope user arte-suave \
  https://e7rfsehko9.execute-api.eu-north-1.amazonaws.com/mcp
```

For a headless client, mint a static token with `bash infra/add-user.sh <name>`
and send it as `Authorization: Bearer <token>`.

Each user signs in with their own gym account and gets an isolated session.
Credentials are stored in SSM under a KMS key only the Lambda role can decrypt.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
LOGIN=... PASSWORD=... uv run arte-suave-mcp   # http://localhost:8080/mcp
ARTESUAVE_LIVE_SMOKE=1 uv run pytest tests/test_live_smoke.py -v
```

`.env` holds real gym credentials and is gitignored.

## Deploy

```bash
bash infra/put-secrets.sh
bash infra/deploy.sh
```

Needs the AWS CLI and `uv`. Set `AWS_PROFILE` to an admin profile for the
target account (otherwise the default credential chain is used); region
defaults to `eu-north-1` (`AWS_REGION`). Pushing to `main` deploys via GitHub
Actions (OIDC).

Portal endpoints and selectors live in `arte_suave_mcp/config.py`;
[`DISCOVERY.md`](DISCOVERY.md) explains how the portal works.
