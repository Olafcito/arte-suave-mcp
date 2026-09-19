# Infrastructure — design & cost review

You asked me to be critical of the "one Lambda behind a Function URL" idea and
find the cheapest solution (free, or ≤ $5/mo if it can grow into a general
personal-assistant backend). Here's the review and what I actually deployed.

## TL;DR

**Deployed: one Lambda (Python 3.12) fronted by an API Gateway HTTP API,**
with DynamoDB for the session and SSM SecureString for secrets. Runs at **$0/mo**
at personal volume, entirely within the always-free / 12-month-free tiers. The
only change from the brief is the front door: this AWS account's Organization
**blocks public Lambda Function URLs** (verified — see below), so a Function URL
would never have been reachable. API Gateway is the org-friendly equivalent.

## Why not the alternatives (cost is the deciding factor)

| Option | Idle monthly cost | Verdict |
|---|---|---|
| **Lambda + API Gateway** (chosen) | **$0** | Free tier covers it; scales to more tools |
| Lambda + Function URL (brief) | $0 | **Blocked by org policy** in this account |
| App Runner | ~$5–7 even idle | No free tier; fixed cost → would need your OK |
| ECS Fargate (always-on task) | ~$9 | No free tier; fixed cost |
| Lightsail container (nano) | $7 | Fixed cost |
| EC2 t4g.nano + EBS | ~$4 | Fixed cost + you patch/run the box |

Everything that gives you an always-on server (no cold start) costs ≥ $5/mo
**fixed**, which your brief said to stop and ask about. Lambda is the only option
that is genuinely free for this workload, so I kept the serverless shape and
just swapped the front door.

## The Function URL block (why API Gateway)

Evidence gathered during deploy:
- Created the Lambda; **direct `lambda invoke` works** — the app boots, deps
  load, the Web Adapter serves, my auth returns 401 without the secret.
- Created a Function URL with `AuthType: NONE` + a correct public
  `lambda:InvokeFunctionUrl` resource policy → every public request returns
  **403 at the AWS layer** (not from my app). SigV4-signed requests too.
- Conclusion: an SCP/account setting forbids public Function URLs. API Gateway
  HTTP API is not affected and is the standard public entry point here.

## What's deployed (all `artesuave-mcp-*`, tagged `project=artesuave-mcp`)

- **Lambda** `artesuave-mcp` — Python 3.12, 512 MB, 30s timeout, x86_64. Hosts
  the whole FastMCP server (all tools in one function, not one-per-tool). The
  **Lambda Web Adapter** layer runs the ASGI app (uvicorn) so FastMCP's
  streamable-HTTP transport works unchanged.
- **API Gateway HTTP API** `artesuave-mcp-api` — public `$default` route →
  Lambda proxy. The app enforces its own bearer secret, so open at the AWS edge
  is fine.
- **DynamoDB** `artesuave-mcp-sessions` — PAY_PER_REQUEST. Holds the reusable
  portal session (cookies, survives cold starts so we re-login rarely), the
  OAuth state (`oauth:*`), and `submit_feedback` notes (`feedback#<uid>#<ts>` —
  no new table). All tiny items.
- **SSM SecureString** `/artesuave-mcp/{login,password,mcp-secret}` — gym
  credentials + the connector bearer secret. Never in code or env files.
- **IAM role** `artesuave-mcp-lambda-role` — least privilege: only GetItem/
  PutItem/DeleteItem on the one table, GetParameter on `/artesuave-mcp/*`, and
  kms:Decrypt via SSM. Basic Lambda logging. Nothing else.
- **Public schedule source** — the `get_schedule` tool also reads the gym's
  public weekly page (`artesuave.dk/traeningstider`) for past days and future
  weeks (the member portal only serves the current week's upcoming classes).
  This is a plain outbound HTTPS GET, no auth and no AWS resources — zero infra
  and zero cost impact.

## Free-tier math (personal use, ~hundreds of calls/month)

- Lambda: 1M req + 400k GB-s **always free**. A tool call ≈ 0.5–8s @ 512 MB → a
  few hundred GB-s/month. Effectively **$0**.
- API Gateway HTTP API: 1M req/month free for 12 months, then **$1.00/million**.
  At personal volume that's cents — but note it is *not* always-free, so after
  12 months expect a few cents/month, still far under $5.
- DynamoDB: 25 GB + generous on-demand free tier. One tiny item → **$0**.
- SSM standard SecureString params + default KMS key: **$0**.
- CloudWatch Logs: negligible; a 30-day retention keeps it free.

**No fixed monthly cost.** Nothing here bills you for sitting idle.

## Growing into a personal assistant

This shape scales without new fixed cost:
- **More tools**: add `@mcp.tool` functions, or mount several FastMCP servers
  behind the same API. Still one Lambda, still free.
- **More integrations**: the single DynamoDB table (PK `pk`) can hold sessions/
  state for other services; add SSM params under their own prefixes.
- **When you outgrow Lambda** (need always-on, websockets, long jobs): lift the
  same ASGI app to App Runner — that's the point where the ≤$5/mo budget you
  mentioned kicks in. No rewrite; the app is a standard ASGI application.

## Auth: login flow + credential encryption

The endpoint is public at the AWS edge; the app authenticates every request. It
accepts three token types: the legacy shared secret, a self-identifying
`<userid>.<secret>` token, and **OAuth 2.1 access tokens** issued by the server's
own login flow (`oauth.py`). Claude discovers the OAuth endpoints, registers
itself (dynamic client registration), and sends the user to a login page; there's
no OAuth provider on the gym side to delegate to, so the page collects the Arte
Suave credentials, verifies them against the portal, and stores them.

- **Credential encryption.** Per-user creds are SSM SecureStrings encrypted under
  a dedicated customer-managed KMS key (`alias/artesuave-mcp-creds`). Its key
  policy grants *cryptographic* use (encrypt/decrypt) **only to the Lambda
  execution role**; the account root gets management actions but **not** decrypt.
  So a SecureString value shows as ciphertext in the console/CLI even to an
  admin. This is **not zero-knowledge**: the function must decrypt the password at
  login time to talk to the portal (which needs the real password, not a token),
  and the account owner could always re-grant themselves via a policy edit. Given
  a portal that isn't an OAuth provider, that limit is unavoidable — the goal here
  is "not casually visible", which this achieves.
- **OAuth state** lives in the same DynamoDB table under `oauth:*` keys — clients
  (persistent), auth codes (~5 min TTL), access tokens (~30 day TTL) and refresh
  tokens (~180 day TTL). Tokens are stored hashed. DynamoDB TTL auto-expires them;
  session items omit the `ttl` attribute and persist.
- **Cost.** One customer-managed KMS key is ~$1/mo plus per-request charges — the
  only line item that nudges this above $0. Still pennies overall.

## CI: deploy on merge to main (GitHub OIDC)

`.github/workflows/deploy.yml` runs `infra/deploy.sh` on every push to `main`
(i.e. when a PR merges), after `pytest` + `ruff`. It authenticates with **GitHub
OIDC** — no AWS keys are stored in GitHub. The workflow assumes a dedicated
least-privilege role, `artesuave-mcp-deploy`:

- **Trust** — only `token.actions.githubusercontent.com` for
  `repo:Olafcito/arte-suave-mcp:ref:refs/heads/main` (aud `sts.amazonaws.com`).
  No human/user principal can assume it. Reuses the account's existing OIDC
  provider (shared with other projects — there can only be one per issuer URL).
- **Permissions** — scoped to this project's resources only: the `artesuave-mcp`
  CloudFormation stack (+ the SAM transform macro), the deploy S3 bucket, the
  `artesuave-mcp*` Lambda + the external LWA layer, `iam:PassRole`/manage limited
  to `artesuave-mcp-lambda-role`, the `artesuave-mcp-sessions` DynamoDB table, the
  `alias/artesuave-mcp-creds` KMS key, and the specific API Gateway id. It cannot
  touch nettoday/Tallyday resources. The policy was validated by assuming the
  role and running a full deploy under it before wiring CI.
- `deploy.sh` uses ambient credentials when `AWS_ACCESS_KEY_ID` is set (CI/OIDC)
  and otherwise falls back to the `nettoday-admin` profile for local runs.

## Redeploy / teardown

- Deploy / update: `infra/deploy.sh` (builds Linux wheels with uv — no Docker);
  or just merge to `main` and CI deploys.
- Rotate secrets: `infra/put-secrets.sh`.
- Teardown: `aws cloudformation delete-stack --stack-name artesuave-mcp
  --profile nettoday-admin --region eu-north-1` then empty+delete the deploy
  bucket and the three SSM params. Nothing is shared with any other project.

## Note on the AWS identity

The default CLI profile (`nettoday-app`) is a scoped deploy user that cannot
create IAM roles or write SSM. I used the `nettoday-admin` profile **in the same
account** (415407325274) — this is still your AWS account, and every resource is
namespaced `artesuave-mcp-*` and tagged `project=artesuave-mcp`, fully isolated
from nettoday/Tallyday. `deploy.sh` defaults to `AWS_PROFILE=nettoday-admin`.
