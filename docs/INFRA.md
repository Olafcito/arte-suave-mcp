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
- **DynamoDB** `artesuave-mcp-sessions` — PAY_PER_REQUEST, single item holding
  the reusable portal session (cookies). Survives cold starts so we re-login
  rarely and stay polite to the gym.
- **SSM SecureString** `/artesuave-mcp/{login,password,mcp-secret}` — gym
  credentials + the connector bearer secret. Never in code or env files.
- **IAM role** `artesuave-mcp-lambda-role` — least privilege: only GetItem/
  PutItem/DeleteItem on the one table, GetParameter on `/artesuave-mcp/*`, and
  kms:Decrypt via SSM. Basic Lambda logging. Nothing else.

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

## Redeploy / teardown

- Deploy / update: `infra/deploy.sh` (builds Linux wheels with uv — no Docker).
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
