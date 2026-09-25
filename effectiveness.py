"""召回成效复盘：统计、版本留档与失联车主名单。

统计、档案与接口分开处理：本模块只负责按召回和范围版本归集成效数据、
冻结旧版本账目以及维护失联名单，HTTP 接口留在 app.py。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from common import ApiError, in_scope, j, now, require_actor

DEFAULT_OVERDUE_DAYS = 90  # 通知发出后超过该天数仍未修复视为超期
ESCALATE_AFTER = 3         # 连续联系未果达到该次数才转监管跟进


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class EffectivenessService:
    """按召回和范围版本归集应修、已修、待通知、失联和超期车辆。"""

    def __init__(self, store):
        self.store, self.conn = store, store.conn

    # ---- 统计 ----

    def stats(self, actor: str | None, role: str | None, recall_id: int, overdue_days: int = DEFAULT_OVERDUE_DAYS) -> dict:
        require_actor(actor, role, {"manufacturer", "regulator"})
        recall = self._recall(recall_id)
        current_version = int(recall["scope_version"])
        current = self._compute_stats(recall, current_version, json.loads(recall["scope_json"]), int(overdue_days))
        archived = [{"scope_version": row["scope_version"], "archived_by": row["archived_by"],
                     "archived_at": row["archived_at"], **json.loads(row["stats_json"])}
                    for row in self.conn.execute("SELECT * FROM stats_archives WHERE recall_id=? ORDER BY scope_version", (recall_id,))]
        return {"recall_id": recall_id, "campaign_code": recall["campaign_code"], "generated_at": now(),
                "overdue_days": int(overdue_days), "current": current, "archived": archived}

    def _compute_stats(self, recall, scope_version: int, scope: dict, overdue_days: int) -> dict:
        repaired_ids = {row["vehicle_id"] for row in self.conn.execute(
            "SELECT vehicle_id FROM repairs WHERE recall_id=? AND status='confirmed'", (recall["id"],))}
        notifications = {row["vehicle_id"]: row for row in self.conn.execute(
            "SELECT * FROM notifications WHERE recall_id=? AND scope_version=?", (recall["id"], scope_version))}
        contact_status = self._contact_status(recall["id"])
        released_at = self._version_released_at(recall, scope_version)
        now_dt = datetime.now(timezone.utc)

        def blank() -> dict:
            return {"due": 0, "repaired": 0, "pending_notification": 0, "unreachable": 0, "overdue": 0}

        totals, by_country, by_model = blank(), {}, {}
        for vehicle in self.conn.execute("SELECT * FROM vehicles ORDER BY id"):
            if not in_scope(vehicle, scope):
                continue
            # 多次转移的车辆只按当前归属统计一次
            repaired = vehicle["id"] in repaired_ids
            notice = notifications.get(vehicle["id"])
            pending = notice is None or notice["status"] == "queued"
            lost = not repaired and contact_status.get(vehicle["id"], {}).get("consecutive", 0) > 0
            basis = _parse(notice["created_at"] if notice else released_at)
            overdue = not repaired and (now_dt - basis).total_seconds() > overdue_days * 86400
            for bucket in (totals, by_country.setdefault(vehicle["country"], blank()), by_model.setdefault(vehicle["model"], blank())):
                bucket["due"] += 1
                bucket["repaired"] += int(repaired)
                bucket["pending_notification"] += int(pending)
                bucket["unreachable"] += int(lost)
                bucket["overdue"] += int(overdue)
        return {"scope_version": scope_version, "totals": totals, "by_country": by_country, "by_model": by_model}

    def _version_released_at(self, recall, scope_version: int) -> str:
        row = self.conn.execute("SELECT created_at FROM scope_changes WHERE recall_id=? AND scope_version=?",
                                (recall["id"], scope_version)).fetchone()
        if row: return row["created_at"]
        row = self.conn.execute("SELECT created_at FROM regulatory_reports WHERE recall_id=? AND scope_version=?",
                                (recall["id"], scope_version)).fetchone()
        return row["created_at"] if row else recall["created_at"]

    # ---- 档案 ----

    def archive_version(self, recall_id: int, scope_version: int, actor: str) -> None:
        """范围调整时冻结旧版本账目；由调用方负责事务。"""
        recall = self._recall(recall_id)
        stats = self._compute_stats(recall, scope_version, self._scope_for_version(recall, scope_version), DEFAULT_OVERDUE_DAYS)
        self.conn.execute("""INSERT INTO stats_archives(recall_id,scope_version,stats_json,archived_by,archived_at)
                             VALUES(?,?,?,?,?)
                             ON CONFLICT(recall_id,scope_version) DO UPDATE SET
                               stats_json=excluded.stats_json, archived_by=excluded.archived_by, archived_at=excluded.archived_at""",
                          (recall_id, scope_version, j(stats), actor, now()))
        self.store.audit(actor, "recall.stats_archive", "recall", recall_id, {"scope_version": scope_version})

    def _scope_for_version(self, recall, scope_version: int) -> dict:
        if int(scope_version) == int(recall["scope_version"]):
            return json.loads(recall["scope_json"])
        row = self.conn.execute("SELECT scope_json FROM scope_changes WHERE recall_id=? AND scope_version=?",
                                (recall["id"], scope_version)).fetchone()
        if not row: raise ApiError(404, "范围版本不存在")
        return json.loads(row["scope_json"])

    # ---- 失联名单 ----

    def record_contact(self, actor: str | None, role: str | None, recall_id: int, vin: str, result: str, note: str = "") -> dict:
        actor = require_actor(actor, role, {"dealer", "manufacturer"})
        if result not in {"unreachable", "reached"}: raise ApiError(400, "联系结果只能是 unreachable 或 reached")
        recall = self._recall(recall_id)
        if recall["state"] != "published": raise ApiError(409, "召回尚未发布")
        vehicle = self._vehicle(vin)
        if not in_scope(vehicle, json.loads(recall["scope_json"])): raise ApiError(409, "车辆不在当前召回范围内")
        repaired = self.conn.execute("SELECT id FROM repairs WHERE recall_id=? AND vehicle_id=? AND status='confirmed'",
                                     (recall_id, vehicle["id"])).fetchone()
        if repaired: raise ApiError(409, "车辆已完成修复，无需再联系")
        with self.conn:
            cur = self.conn.execute("INSERT INTO contact_attempts(recall_id,vehicle_id,result,note,actor,created_at) VALUES(?,?,?,?,?,?)",
                                    (recall_id, vehicle["id"], result, note, actor, now()))
            self.store.audit(actor, "contact.attempt", "recall", recall_id, {"vin": vehicle["vin"], "result": result})
            status = self._contact_status(recall_id)[vehicle["id"]]
            if result == "unreachable" and status["consecutive"] == ESCALATE_AFTER:
                self.store.audit(actor, "contact.escalate", "recall", recall_id,
                                 {"vin": vehicle["vin"], "attempts": status["consecutive"], "note": "三次联系未果，转监管跟进"})
        return {"id": cur.lastrowid, "recall_id": recall_id, "vin": vehicle["vin"], "result": result,
                "consecutive_failures": status["consecutive"],
                "status": "cleared" if result == "reached" else ("escalated" if status["consecutive"] >= ESCALATE_AFTER else "tracking")}

    def unreachable(self, actor: str | None, role: str | None, recall_id: int) -> dict:
        require_actor(actor, role, {"manufacturer", "regulator"})
        recall = self._recall(recall_id)
        scope = json.loads(recall["scope_json"])
        repaired_ids = {row["vehicle_id"] for row in self.conn.execute(
            "SELECT vehicle_id FROM repairs WHERE recall_id=? AND status='confirmed'", (recall_id,))}
        contact_status = self._contact_status(recall_id)
        items = []
        for vehicle in self.conn.execute("SELECT * FROM vehicles ORDER BY vin"):
            status = contact_status.get(vehicle["id"])
            if not status or status["consecutive"] < 1: continue  # 联系成功即退出名单
            if vehicle["id"] in repaired_ids or not in_scope(vehicle, scope): continue  # 完成修复即退出名单
            items.append({"vin": vehicle["vin"], "owner_name": vehicle["owner_name"], "country": vehicle["country"],
                          "model": vehicle["model"], "attempts": status["attempts"], "consecutive_failures": status["consecutive"],
                          "escalated": status["consecutive"] >= ESCALATE_AFTER, "escalated_at": status["escalated_at"],
                          "last_attempt_at": status["last_at"], "contacts": status["history"]})
        return {"recall_id": recall_id, "scope_version": recall["scope_version"], "unreachable_count": len(items), "vehicles": items}

    def _contact_status(self, recall_id: int) -> dict:
        status = {}
        for attempt in self.conn.execute("SELECT * FROM contact_attempts WHERE recall_id=? ORDER BY id", (recall_id,)):
            entry = status.setdefault(attempt["vehicle_id"],
                                      {"attempts": 0, "consecutive": 0, "last_at": None, "escalated_at": None, "history": []})
            entry["attempts"] += 1
            entry["last_at"] = attempt["created_at"]
            entry["history"].append({"result": attempt["result"], "note": attempt["note"],
                                     "actor": attempt["actor"], "at": attempt["created_at"]})
            if attempt["result"] == "reached":
                entry["consecutive"], entry["escalated_at"] = 0, None
            else:
                entry["consecutive"] += 1
                if entry["consecutive"] >= ESCALATE_AFTER and entry["escalated_at"] is None:
                    entry["escalated_at"] = attempt["created_at"]
        return status

    def _recall(self, recall_id: int):
        row = self.conn.execute("SELECT * FROM recalls WHERE id=?", (recall_id,)).fetchone()
        if not row: raise ApiError(404, "召回不存在")
        return row

    def _vehicle(self, vin: str):
        row = self.conn.execute("SELECT * FROM vehicles WHERE vin=?", (vin.upper().strip(),)).fetchone()
        if not row: raise ApiError(404, "车辆不存在")
        return row
