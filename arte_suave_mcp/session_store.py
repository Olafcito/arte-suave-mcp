"""Session persistence: in-memory cache plus a pluggable backend.

Backends: file (local dev) and DynamoDB (Lambda). The stored blob is a dict of
cookie name -> value plus obtained_at. Cookies are secrets, so the file backend
lives outside the repo and DynamoDB is locked down by IAM.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Protocol


class SessionBackend(Protocol):
    def load(self) -> dict | None: ...
    def save(self, blob: dict) -> None: ...
    def clear(self) -> None: ...


class MemoryBackend:
    def __init__(self) -> None:
        self._blob: dict | None = None

    def load(self) -> dict | None:
        return self._blob

    def save(self, blob: dict) -> None:
        self._blob = blob

    def clear(self) -> None:
        self._blob = None


class FileBackend:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, blob: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(blob), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class DynamoBackend:
    """Single-item session store. Table has a string PK 'pk'."""

    def __init__(self, table_name: str, key: str = "session") -> None:
        import boto3  # lazy: only needed in AWS

        self.key = key
        self.table = boto3.resource("dynamodb").Table(table_name)

    def load(self) -> dict | None:
        resp = self.table.get_item(Key={"pk": self.key})
        item = resp.get("Item")
        if not item or "blob" not in item:
            return None
        try:
            return json.loads(item["blob"])
        except json.JSONDecodeError:
            return None

    def save(self, blob: dict) -> None:
        self.table.put_item(Item={"pk": self.key, "blob": json.dumps(blob)})

    def clear(self) -> None:
        self.table.delete_item(Key={"pk": self.key})


def build_backend(key: str = "session") -> SessionBackend:
    """Pick a backend from env: DynamoDB in Lambda, file locally, else memory.

    `key` namespaces the stored session so each user gets an isolated record.
    """
    table = os.environ.get("ARTESUAVE_SESSION_TABLE")
    if table:
        return DynamoBackend(table, key=key)
    path = os.environ.get("ARTESUAVE_SESSION_FILE")
    if path:
        if key != "session":
            p = Path(path)
            path = str(p.with_name(f"{p.stem}.{key}{p.suffix}"))
        return FileBackend(path)
    return MemoryBackend()


class SessionStore:
    """Two-tier cache: process memory in front of a durable backend."""

    def __init__(self, backend: SessionBackend | None = None) -> None:
        self.backend = backend or build_backend()
        self._mem: dict | None = None

    def get_cookies(self) -> dict[str, str]:
        blob = self._mem or self.backend.load()
        if blob:
            self._mem = blob
            return dict(blob.get("cookies", {}))
        return {}

    def put_cookies(self, cookies: dict[str, str]) -> None:
        blob = {"cookies": cookies, "obtained_at": int(time.time())}
        self._mem = blob
        self.backend.save(blob)

    def clear(self) -> None:
        self._mem = None
        self.backend.clear()
