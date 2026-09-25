"""共享基础：时间、JSON、接口错误与召回范围匹配。"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message); self.status, self.message = status, message


def require_actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
    if not actor: raise ApiError(401, "缺少身份")
    if role not in allowed: raise ApiError(403, "角色无权执行此操作")
    return actor


def in_scope(vehicle, scope: dict) -> bool:
    return (vehicle["model"] in scope.get("models", []) and int(vehicle["model_year"]) in scope.get("model_years", [])
            and any(vehicle["vin"].startswith(prefix.upper()) for prefix in scope.get("vin_prefixes", []))
            and (vehicle["country"] in scope.get("countries", []) or vehicle["origin_country"] in scope.get("countries", [])))
