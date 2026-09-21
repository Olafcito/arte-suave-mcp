"""Read and mark Arte Suave MCP feedback in the sessions table.

    uv run python .claude/skills/handling-feedback/feedback.py list [--all]
    uv run python .claude/skills/handling-feedback/feedback.py handle <pk> "<resolution>"

`list` prints open items (add --all for handled ones too), oldest first.
`handle` sets handled/handled_at/resolution on one item; items are never deleted.
AWS: credentials from AWS_PROFILE when set, else the default credential chain;
region eu-north-1 (override with AWS_REGION).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

import boto3
from boto3.dynamodb.conditions import Attr

TABLE = "artesuave-mcp-sessions"
REGION = os.environ.get("AWS_REGION", "eu-north-1")
PROFILE = os.environ.get("AWS_PROFILE") or None  # None -> default credential chain


def _table():
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    return session.resource("dynamodb").Table(TABLE)


def _scan(include_handled: bool) -> list[dict]:
    cond = Attr("pk").begins_with("feedback#")
    if not include_handled:
        cond = cond & (Attr("handled").not_exists() | Attr("handled").eq(False))
    items: list[dict] = []
    kwargs: dict = {"FilterExpression": cond}
    while True:
        page = _table().scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return sorted(items, key=lambda i: int(i["created_at"]))
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _when(ts) -> str:
    if ts is None:
        return "?"
    return dt.datetime.fromtimestamp(int(ts), dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def cmd_list(args: argparse.Namespace) -> None:
    items = _scan(args.all)
    if not items:
        print("no feedback" if args.all else "no open feedback")
        return
    for it in items:
        state = "handled" if it.get("handled") else "OPEN"
        print(f"== {it['pk']}  [{state}]  {_when(it.get('created_at'))}")
        if it.get("context"):
            print(f"context: {it['context']}")
        print(it.get("message", ""))
        if it.get("handled"):
            print(f"resolution ({_when(it.get('handled_at'))}): {it.get('resolution')}")
        print()


def cmd_handle(args: argparse.Namespace) -> None:
    _table().update_item(
        Key={"pk": args.pk},
        UpdateExpression="SET handled = :t, handled_at = :now, resolution = :r",
        ConditionExpression="attribute_exists(pk)",
        ExpressionAttributeValues={":t": True, ":now": int(time.time()), ":r": args.resolution},
    )
    print(f"{args.pk} marked handled: {args.resolution}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Danish names on Windows
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="print feedback items")
    ls.add_argument("--all", action="store_true", help="include handled items")
    ls.set_defaults(fn=cmd_list)
    h = sub.add_parser("handle", help="mark one item handled")
    h.add_argument("pk", help="feedback#<user_id>#<ts>")
    h.add_argument("resolution", help='e.g. "Fixed in PR #6" or "features.md F3"')
    h.set_defaults(fn=cmd_handle)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
