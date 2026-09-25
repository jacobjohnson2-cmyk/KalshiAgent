"""Append-only JSONL audit log of every order attempt and its outcome."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any


class AuditLog:
    def __init__(self, path: str, env: str):
        self.path = path
        self.env = env
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "env": self.env,
                  "event": event, **fields}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
