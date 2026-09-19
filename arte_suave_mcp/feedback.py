"""Store feedback the AI submits on the user's behalf.

When the assistant does something the user disagrees with, it can call the
``submit_feedback`` tool; the note lands here so Simon can review it later. We
reuse the sessions DynamoDB table (no new infra) under ``feedback#<user>#<ts>``
keys, which sort chronologically per user. Locally (no table configured) we keep
them in a process list so the tool still works in dev and tests.
"""

from __future__ import annotations

import os
import time

MAX_MESSAGE = 4000
MAX_CONTEXT = 4000

# Local/dev fallback when no DynamoDB table is configured.
_memory: list[dict] = []
_table_ref = None


def _table():
    global _table_ref
    if _table_ref is None:
        import boto3

        _table_ref = boto3.resource("dynamodb").Table(
            os.environ["ARTESUAVE_SESSION_TABLE"]
        )
    return _table_ref


def store(user_id: str, message: str, context: str | None = None) -> dict:
    """Persist one feedback note and return its stored record (id + timestamp)."""
    ts = int(time.time())
    item = {
        "pk": f"feedback#{user_id}#{ts}",
        "kind": "feedback",
        "user_id": user_id,
        "message": (message or "").strip()[:MAX_MESSAGE],
        "created_at": ts,
    }
    if context:
        item["context"] = context.strip()[:MAX_CONTEXT]
    if os.environ.get("ARTESUAVE_SESSION_TABLE"):
        _table().put_item(Item=item)
    else:
        _memory.append(item)
    return {"id": item["pk"], "created_at": ts}
