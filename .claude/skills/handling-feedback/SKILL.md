---
name: handling-feedback
description: Use when asked to check, read, triage, or fix Arte Suave MCP user feedback — anything submitted via the submit_feedback tool, "latest feedback", or a complaint that a tool gave a wrong or confusing answer.
---

# Handling Arte Suave MCP feedback

## Where feedback lives

DynamoDB table `artesuave-mcp-sessions` (account 415407325274, `eu-north-1`),
items whose `pk` starts with `feedback#` (`feedback#<user_id>#<unix_ts>`), with
`message`, `context`, `user_id`, `created_at`.

Fetch:

```bash
aws dynamodb scan --table-name artesuave-mcp-sessions \
  --filter-expression "begins_with(pk, :p)" \
  --expression-attribute-values '{":p":{"S":"feedback#"}}' \
  --region eu-north-1 --profile nettoday-admin
```

## Process

1. Read every open feedback item; `context` says what prompted it.
2. Locate the behavior: tool response shape lives in `arte_suave_mcp/service.py`
   (`_present_class`, `_schedule_notes`), HTML parsing in `parsers.py`,
   selectors/aliases in `config.py`, tool docstrings in `server.py`.
3. Fix with TDD (**REQUIRED SUB-SKILL:** superpowers:test-driven-development).
   Tests + redacted HTML fixtures are in `tests/`.
4. Verify: `uv run pytest` and `uv run ruff check .` must pass.
5. Ship: branch → PR → merge to `main`. Merging deploys automatically
   (GitHub Actions, OIDC). Confirm the run is green, then call the live
   `health_check` tool if the change touched fetching or parsing.
6. Update the tool docstring in `server.py` whenever a response shape changes —
   that text is what the client model sees.
7. Delete the processed `feedback#...` items so the table only holds open
   feedback:

   ```bash
   aws dynamodb delete-item --table-name artesuave-mcp-sessions \
     --key '{"pk":{"S":"feedback#<user_id>#<ts>"}}' \
     --region eu-north-1 --profile nettoday-admin
   ```

## Owner's interface principles

- Users never see server plumbing: no `source`, no null fields, no
  `false`-by-default flags. Strip noise in `_present_class`, not in the client.
- A per-entry flag only where it's meaningful (e.g. `bookable`, `signed_up` on
  released classes); one plain top-level `note` instead of repeated per-row
  fields (e.g. "classes from X onward aren't released yet").
- When in doubt, make the tool answer the user's question directly rather than
  returning data the assistant must reconcile.
