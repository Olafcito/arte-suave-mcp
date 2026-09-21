---
name: handling-feedback
description: Use when asked to check, read, triage, or fix Arte Suave MCP user feedback — anything submitted via the submit_feedback tool, "latest feedback", or a complaint that a tool gave a wrong or confusing answer.
---

# Handling Arte Suave MCP feedback

## Where feedback lives

DynamoDB table `artesuave-mcp-sessions` (`eu-north-1`),
items whose `pk` starts with `feedback#` (`feedback#<user_id>#<unix_ts>`), with
`message`, `context`, `user_id`, `created_at`, and `handled` (BOOL).

`handled` is the record of state: new feedback is stored with `handled: false`;
it flips to `true` (plus `handled_at` and a one-line `resolution`) once it has
been reviewed and either fixed or written up in `features.md`. Items are never
deleted, so the table keeps the history.

Read it with the script next to this file (items predating the flag have no
`handled` and count as open):

```bash
uv run python .claude/skills/handling-feedback/feedback.py list        # open items
uv run python .claude/skills/handling-feedback/feedback.py list --all  # incl. resolutions
```

## Process

1. Read every open feedback item; `context` says what prompted it. For each,
   decide: fix it now (steps 2-6), or add it to `features.md` (repo root) as a
   `planned` entry with the feedback `pk` as its source and a short spec. Either
   outcome counts as handled (step 7). Anything bigger than a one-line fix gets
   its spec in `features.md` first, then is executed from that spec.
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
7. Mark each reviewed item handled once it is fixed or recorded in
   `features.md`. `resolution` says which ("Fixed in PR #n", "features.md F3",
   or why nothing changed). Never delete feedback items.

   ```bash
   uv run python .claude/skills/handling-feedback/feedback.py handle "feedback#<user_id>#<ts>" "Fixed in PR #n"
   ```

   When a `features.md` entry ships, update its status there; the feedback item
   stays as it is.

## Owner's interface principles

- Users never see server plumbing: no `source`, no null fields, no
  `false`-by-default flags. Strip noise in `_present_class`, not in the client.
- A per-entry flag only where it's meaningful (e.g. `bookable`, `signed_up` on
  released classes); one plain top-level `note` instead of repeated per-row
  fields (e.g. "classes from X onward aren't released yet").
- When in doubt, make the tool answer the user's question directly rather than
  returning data the assistant must reconcile.
