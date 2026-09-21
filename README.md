# 🥊 arte-suave-mcp

[![deploy](https://github.com/Olafcito/arte-suave-mcp/actions/workflows/deploy.yml/badge.svg)](https://github.com/Olafcito/arte-suave-mcp/actions/workflows/deploy.yml)
![python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![fastmcp](https://img.shields.io/badge/FastMCP-server-6E56CF)
![aws lambda](https://img.shields.io/badge/AWS-Lambda-FF9900?logo=awslambda&logoColor=white)
![region](https://img.shields.io/badge/region-eu--north--1-232F3E)

MCP server for the [Arte Suave](https://artesuave.dk) gym portal in Copenhagen.

I built it so I can manage my muay thai classes from my AI provider and sync
them with my calendar, as part of a personal AI assistant that runs my
training schedule. Not affiliated with the gym.

```mermaid
flowchart TB
    U(["🧑 You"]) -- "book me in on Tuesday" --> A["🤖 Claude / ChatGPT"]
    A -- "MCP" --> L["λ arte-suave-mcp"]
    A -- "class times" --> C["📅 Your calendar"]
    L -- "schedule, book, cancel" --> P["🏋️ Arte Suave portal"]
```

## 🧱 Tech stack

- **Python 3.12** and [FastMCP](https://gofastmcp.com), served by uvicorn over streamable HTTP.
- **httpx** and **selectolax** to log in to the portal and parse its HTML. The portal has no API.
- **AWS Lambda** behind an API Gateway HTTP API, running the ASGI app through the Lambda Web Adapter.
- **DynamoDB** for sessions, OAuth tokens and feedback. **S3** for training pictures.
- **SSM Parameter Store** and **KMS** for user credentials. Only the Lambda role can decrypt them.
- **CloudFormation**, deployed by GitHub Actions on every merge to `main`.

## 🚀 Using it

### Connect

Endpoint: `https://e7rfsehko9.execute-api.eu-north-1.amazonaws.com/mcp`

Add it to your assistant and sign in with your own Arte Suave account on the
login page that opens. Each user gets an isolated session, and credentials
are stored in SSM under a KMS key only the Lambda can decrypt.

| Client | How |
|---|---|
| claude.ai, Claude Desktop | Settings → Connectors → Add custom connector → paste the endpoint |
| ChatGPT (plugins) | Settings → Plugins → Add → paste the endpoint |
| Claude Code | `claude mcp add --transport http --scope user arte-suave <endpoint>` |

For a headless client the owner can mint a static token with
`bash infra/add-user.sh <name>`, sent as `Authorization: Bearer <token>`.

### 💬 Things to ask

> "What muay thai classes are there this week?"

> "Sign me up for the Tuesday and Thursday 17:00 classes and put them in my calendar."

> "What classes am I signed up to?"

> "Cancel Thursday's class."

> "How many times did I train last month, and which disciplines?"

> "Who teaches No Gi on Wednesday?"

Discipline names match loosely. "kickboxing", "muay thai" and "K1" all resolve
to thai boxing, and "MMA Stand up" shows up under thai boxing too. Calendar
entries come from your assistant's own calendar connector. This server only
supplies the class times.

Booking is real. A booking or cancellation is confirmed by reading your
bookings back from the portal. The gym releases next week's classes on
Sunday, and until then the tool shows the planned schedule with a note that
those days cannot be booked yet.

### 🧰 Tools

| Tool | What it does |
|---|---|
| `get_schedule(discipline?, date_from?, date_to?)` | Classes with trainer, time, location and spots. Released days are live and bookable, other days come from the public weekly plan. |
| `get_my_bookings()` | Your current bookings. |
| `get_history(date_from?, date_to?)` | Attendance counts, hours and a per discipline breakdown. |
| `book_class(class_id)` / `cancel_booking(booking_id)` | Book or cancel, confirmed by reading bookings back. |
| `upload_training_pic(image_base64?, note?)` / `get_training_pics()` | Store and list training pictures. An experiment, see `features.md` F2. |
| `submit_feedback(message, context?)` | Leaves a note for the owner. Every note is read and either fixed or written up in `features.md`. |
| `health_check()` | Login plus parser checks. |
| `debug_fetch(target)` | Sanitized raw HTML for when the site changes. |

## 🛠️ Contributing

### 🗺️ Architecture

```mermaid
flowchart TB
    S["server.py<br/>tools + auth guard"] --> V["service.py<br/>tool logic, response shaping"]
    V --> P["parsers.py<br/>HTML → models"]
    V --> C["client.py + waf.py<br/>login, session, proof of work"]
    P --> K["config.py<br/>URLs, selectors, aliases"]
    C --> K
    S --> O["oauth.py + creds.py<br/>who is calling"]
    V --> D[("DynamoDB<br/>sessions, oauth, feedback")]
    V --> B[("S3<br/>training pics")]
    O --> M[("SSM + KMS<br/>credentials")]
```

- [`DISCOVERY.md`](DISCOVERY.md) explains how the portal works and why the
  code does what it does.
- `features.md` is the backlog. Anything bigger than a one line fix gets a spec
  there first.

### Set up and test

```bash
uv sync
uv run pytest
uv run ruff check .
```

The tests run offline against redacted HTML fixtures, so this is all you need
to fix a parser, add a discipline alias or reshape a response. Pull requests
for the Python package are welcome.

### Run it locally

You need an Arte Suave membership, because the server logs in with your own
account. You do not need any AWS resources. Without them sessions live in
memory, OAuth is off and the local endpoint is open. Only the picture tools
need the S3 bucket.

```bash
export LOGIN=you@example.com PASSWORD=...
uv run arte-suave-mcp   # http://localhost:8080/mcp
```

The server reads `LOGIN` and `PASSWORD` from the environment. The live smoke
test reads them from `.env` instead, which is gitignored:

```bash
ARTESUAVE_LIVE_SMOKE=1 uv run pytest tests/test_live_smoke.py -v   # read only
```

### ☁️ Deploy (owner)

```bash
bash infra/put-secrets.sh
bash infra/deploy.sh
```

Needs the AWS CLI, `uv` and your own AWS account. The stack in `infra/`
creates the DynamoDB table, S3 bucket, KMS key and Lambda. The GitHub deploy
role is created by hand outside this repo. Set `AWS_PROFILE` to an admin
profile for the target account, otherwise the default credential chain is
used. Region defaults to `eu-north-1` (`AWS_REGION`). Merging to `main`
deploys through GitHub Actions with OIDC. Infra changes are best raised as an
issue first.
