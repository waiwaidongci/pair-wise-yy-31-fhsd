"""Shared primitives for the recall tracker: clock, JSON helpers, errors."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message
