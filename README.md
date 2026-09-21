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
    A -- "MCP over HTTPS" --> L["λ arte-suave-mcp"]
    A -- "class times" --> C["📅 Your calendar"]
    L -- "login, schedule, book" --> P["🏋️ am.artesuave.dk"]
    L -. "planned weeks" .-> W["🌐 artesuave.dk public plan"]
```

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

## 🛠️ Developing

### Prerequisites

| To | You need |
|---|---|
| Fix a parser, add an alias, reshape a response | `uv` and Python 3.12. The tests run offline against redacted HTML fixtures. |
| Run the server locally | An Arte Suave membership. The server logs in with your own account. |
| Deploy | Your own AWS account. The GitHub deploy role is created by hand outside this repo, so `infra/` is the owner's deployment. |

Pull requests for the Python package are welcome. Infra changes are best
raised as an issue first.

### Set up and test

```bash
uv sync
uv run pytest
uv run ruff check .
```

### Run it locally

```bash
LOGIN=... PASSWORD=... uv run arte-suave-mcp   # http://localhost:8080/mcp
ARTESUAVE_LIVE_SMOKE=1 uv run pytest tests/test_live_smoke.py -v   # read only, needs .env
```

`.env` holds real gym credentials and is gitignored.

### 🧱 Tech stack

| Layer | Choice | Why |
|---|---|---|
| MCP server | [FastMCP](https://gofastmcp.com) over streamable HTTP, served by uvicorn | Tools are plain Python functions with docstrings. The docstrings are what the client model reads. |
| Portal access | httpx and selectolax | The portal is a server rendered PHP site behind a WAF proof of work, with no API. The server logs in, clears the challenge and parses HTML. |
| Models | pydantic | Typed result shapes and HTML sanitizing. |
| Compute | One AWS Lambda (Python 3.12) running uvicorn through the Lambda Web Adapter, behind an API Gateway HTTP API | No container and no always-on server. The deploy is a zip built by `uv`. |
| State | DynamoDB, one table | Portal sessions, OAuth clients, codes and tokens with TTL, feedback and picture metadata. |
| Credentials | SSM Parameter Store SecureStrings under a dedicated KMS key | Only the Lambda role can decrypt. The account admin can manage the key but cannot read gym passwords from the console. |
| Auth | Built in OAuth 2.1 server with PKCE, plus bearer tokens for headless clients | The gym is not an identity provider, so this server is one. It verifies your login against the portal and mints its own tokens. |
| Pictures | Private S3 bucket, presigned URLs | Photos never pass through the model. |
| Infra and CI | CloudFormation with the SAM transform, GitHub Actions with OIDC | Merging to `main` runs tests and deploys. No long lived AWS keys in GitHub. |
| Dev tools | `uv`, pytest, ruff, Playwright | Playwright is only used for portal discovery, see `DISCOVERY.md`. |

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

### ☁️ Deploy (owner)

```bash
bash infra/put-secrets.sh
bash infra/deploy.sh
```

Needs the AWS CLI and `uv`. Set `AWS_PROFILE` to an admin profile for the
target account, otherwise the default credential chain is used. Region
defaults to `eu-north-1` (`AWS_REGION`). Merging to `main` deploys through
GitHub Actions with OIDC.
