# arte-suave-mcp

- `uv run pytest` / `uv run ruff check .` — must pass before a PR. The `VIRTUAL_ENV ... does not match` warning is harmless.
- Live read-only check: `ARTESUAVE_LIVE_SMOKE=1 uv run pytest tests/test_live_smoke.py -v` (needs `.env`; use `load_dotenv(".env")` from a script file, not stdin).
- AWS: profile `nettoday-admin`, region `eu-north-1`. Merging to `main` deploys (GitHub Actions OIDC, `infra/deploy.sh`).
- `main` has a review-required ruleset: open a PR; the owner merges or approves `--admin`.
- Spec first: anything beyond a one-line fix gets an entry in `features.md`, then TDD from that spec.
- Feedback is marked `handled` in DynamoDB, never deleted — see the `handling-feedback` skill.
- Tool docstrings in `server.py` are what the client model reads; update them with any response-shape change.
- Responses: no server plumbing, no null/false-by-default fields; per-row flags only where meaningful, one top-level `note` otherwise.
- Windows Git Bash: backslashes in heredoc Python get collapsed — use the Edit tool or write scripts to a file. LF→CRLF warnings are harmless.
