"""Domain service: recall workflow, repairs and effectiveness review."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from archives import ContactArchive, ScopeArchive
from common import ApiError, j, now
from stats import EffectivenessStats
from store import Store


class RecallService:
    def __init__(self, store: Store):
        self.store, self.conn = store, store.conn
        self.scope_archive = ScopeArchive(self.conn)
        self.contact_archive = ContactArchive(self.conn)
        self.stats = EffectivenessStats(self.conn)

    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor: raise ApiError(401, "缺少身份")
        if role not in allowed: raise ApiError(403, "角色无权执行此操作")
        return actor

    def _row(self, table: str, identity: object, column: str = "id") -> sqlite3.Row:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (identity,)).fetchone()
        if not row: raise ApiError(404, "对象不存在")
        return row

    def register_dealer(self, actor: str | None, role: str | None, code: str, name: str, country: str) -> dict:
        actor = self._actor(actor, role, {"regulator"})
        if not code or not country: raise ApiError(400, "维修网点代号和国家不能为空")
        try:
            with self.conn:
                cur = self.conn.execute("INSERT INTO dealers(code,name,country) VALUES(?,?,?)", (code, name, country))
                self.store.audit(actor, "dealer.register", "dealer", cur.lastrowid, {"code": code, "country": country})
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "维修网点代号已存在") from exc
        return {"id": cur.lastrowid, "code": code, "name": name, "country": country, "active": True}

    def register_vehicle(self, actor: str | None, role: str | None, vin: str, model: str, model_year: int, country: str, owner_name: str) -> dict:
        actor = self._actor(actor, role, {"manufacturer", "regulator"})
        vin = vin.upper().strip()
        if len(vin) < 5 or not model or int(model_year) < 1900: raise ApiError(400, "车辆识别信息不完整")
        try:
            with self.conn:
                cur = self.conn.execute("INSERT INTO vehicles(vin,model,model_year,country,origin_country,owner_name,updated_at) VALUES(?,?,?,?,?,?,?)",
                                        (vin, model, int(model_year), country, country, owner_name, now()))
                self.store.audit(actor, "vehicle.register", "vehicle", cur.lastrowid, {"vin": vin, "country": country})
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "车辆识别码已存在") from exc
        return {"id": cur.lastrowid, "vin": vin, "model": model, "model_year": model_year, "country": country, "owner_name": owner_name}

    def transfer_vehicle(self, actor: str | None, role: str | None, vin: str, country: str, owner_name: str) -> dict:
        actor = self._actor(actor, role, {"dealer", "regulator"})
        vehicle = self._row("vehicles", vin.upper(), "vin")
        with self.conn:
            self.conn.execute("UPDATE vehicles SET country=?,owner_name=?,updated_at=? WHERE id=?", (country, owner_name, now(), vehicle["id"]))
            self.store.audit(actor, "vehicle.transfer", "vehicle", vehicle["id"], {"old_country": vehicle["country"], "new_country": country, "owner_name": owner_name})
        return dict(self._row("vehicles", vehicle["id"]))

    def create_recall(self, actor: str | None, role: str | None, campaign_code: str, title: str, scope: dict, remedy: dict) -> dict:
        actor = self._actor(actor, role, {"manufacturer"})
        self._validate_scope(scope)
        if not campaign_code.strip() or not remedy.get("description") or not remedy.get("version"):
            raise ApiError(400, "召回活动编号和修复方案不能为空")
        self._validate_deadline(remedy.get("deadline"))
        stamp = now()
        try:
            with self.conn:
                cur = self.conn.execute("""INSERT INTO recalls(manufacturer,campaign_code,title,scope_json,remedy_version,remedy_json,state,created_by,created_at,updated_at)
                                         VALUES(?,?,?,?,?,?, 'draft',?,?,?)""",
                                        (actor, campaign_code, title, j(scope), int(remedy["version"]), j(remedy), actor, stamp, stamp))
                self.store.audit(actor, "recall.create", "recall", cur.lastrowid, {"campaign_code": campaign_code, "remedy_version": remedy["version"]})
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "召回活动编号已存在") from exc
        return self._recall_dict(self._row("recalls", cur.lastrowid))

    @staticmethod
    def _validate_deadline(deadline: object) -> None:
        if not deadline:
            return
        try:
            datetime.strptime(str(deadline), "%Y-%m-%d")
        except ValueError as exc:
            raise ApiError(400, "修复期限需为 YYYY-MM-DD 日期") from exc

    def submit_recall(self, actor: str | None, role: str | None, recall_id: int, expected_version: int) -> dict:
        actor = self._actor(actor, role, {"manufacturer"})
        recall = self._row("recalls", recall_id)
        if recall["created_by"] != actor: raise ApiError(403, "只能提交本机构创建的召回")
        if recall["state"] != "draft": raise ApiError(409, "只有草稿可以提交")
        return self._recall_state_change(recall, "submitted", expected_version, actor, "提交监管审核")

    def review_recall(self, actor: str | None, role: str | None, recall_id: int, decision: str, expected_version: int, note: str = "") -> dict:
        actor = self._actor(actor, role, {"regulator"})
        if decision not in {"publish", "return"}: raise ApiError(400, "决定只能是 publish 或 return")
        recall = self._row("recalls", recall_id)
        if recall["state"] != "submitted": raise ApiError(409, "只有已提交召回可以审核")
        if int(expected_version) != int(recall["revision"]): raise ApiError(409, "召回已被修改，请刷新版本")
        state = "published" if decision == "publish" else "returned"
        result = self._recall_state_change(recall, state, expected_version, actor, note)
        if state == "published":
            self._create_release_artifacts(recall["id"], int(recall["scope_version"]), actor)
            result = self._recall_dict(self._row("recalls", recall_id))
        return result

    def _recall_state_change(self, recall: sqlite3.Row, state: str, expected_version: int, actor: str, note: str) -> dict:
        if int(expected_version) != int(recall["revision"]): raise ApiError(409, "召回版本冲突")
        with self.conn:
            cur = self.conn.execute("UPDATE recalls SET state=?,revision=revision+1,review_note=?,updated_at=? WHERE id=? AND revision=?",
                                    (state, note, now(), recall["id"], expected_version))
            if cur.rowcount != 1: raise ApiError(409, "并发更新冲突")
            self.store.audit(actor, f"recall.{state}", "recall", recall["id"], {"note": note, "scope_version": recall["scope_version"]})
        return self._recall_dict(self._row("recalls", recall["id"]))

    def change_scope(self, actor: str | None, role: str | None, recall_id: int, scope: dict, expected_version: int) -> dict:
        actor = self._actor(actor, role, {"manufacturer"})
        self._validate_scope(scope)
        recall = self._row("recalls", recall_id)
        if recall["manufacturer"] != actor: raise ApiError(403, "只能调整本机构的召回范围")
        if recall["state"] != "published": raise ApiError(409, "只有已发布召回可以调整范围")
        if int(expected_version) != int(recall["revision"]): raise ApiError(409, "召回已被修改，请刷新版本")
        scope_version = int(recall["scope_version"]) + 1
        with self.conn:
            self.conn.execute("UPDATE recalls SET scope_json=?,scope_version=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                              (j(scope), scope_version, now(), recall_id, expected_version))
            self.conn.execute("INSERT INTO scope_changes(recall_id,scope_version,scope_json,created_by,created_at) VALUES(?,?,?,?,?)",
                              (recall_id, scope_version, j(scope), actor, now()))
            self.store.audit(actor, "recall.scope_change", "recall", recall_id, {"scope_version": scope_version, "scope": scope})
        self._create_release_artifacts(recall_id, scope_version, actor)
        return self._recall_dict(self._row("recalls", recall_id))

    def add_parts(self, actor: str | None, role: str | None, recall_id: int, dealer_id: int, remedy_version: int, quantity: int) -> dict:
        actor = self._actor(actor, role, {"manufacturer", "regulator"})
        if quantity <= 0: raise ApiError(400, "入库数量必须大于零")
        recall = self._row("recalls", recall_id); dealer = self._row("dealers", dealer_id)
        if recall["state"] not in {"published", "submitted"}: raise ApiError(409, "召回尚未进入可备件状态")
        with self.conn:
            self.conn.execute("""INSERT INTO parts(recall_id,dealer_id,remedy_version,available) VALUES(?,?,?,?)
                               ON CONFLICT(recall_id,dealer_id,remedy_version) DO UPDATE SET available=available+excluded.available""",
                              (recall_id, dealer_id, remedy_version, quantity))
            self.store.audit(actor, "parts.add", "recall", recall_id, {"dealer_id": dealer_id, "quantity": quantity, "remedy_version": remedy_version})
        row = self.conn.execute("SELECT * FROM parts WHERE recall_id=? AND dealer_id=? AND remedy_version=?", (recall_id, dealer_id, remedy_version)).fetchone()
        return dict(row)

    def report_repair(self, actor: str | None, role: str | None, recall_id: int, vin: str, dealer_id: int, remedy_version: int, evidence_hash: str, evidence_consistent: bool, border_permit: str = "", idempotency_key: str = "") -> dict:
        actor = self._actor(actor, role, {"dealer"})
        if not idempotency_key or not evidence_hash: raise ApiError(400, "证据哈希和幂等键不能为空")
        recall = self._row("recalls", recall_id)
        if recall["state"] != "published": raise ApiError(409, "召回尚未发布")
        if int(remedy_version) != int(recall["remedy_version"]): raise ApiError(409, "维修方案版本不是当前版本")
        vehicle = self._row("vehicles", vin.upper(), "vin")
        dealer = self._row("dealers", dealer_id)
        if not dealer["active"]: raise ApiError(409, "维修网点已停用")
        existing = self.conn.execute("SELECT * FROM repairs WHERE recall_id=? AND vehicle_id=? AND idempotency_key=?", (recall_id, vehicle["id"], idempotency_key)).fetchone()
        if existing: return dict(existing)
        duplicate = self.conn.execute("SELECT id FROM repairs WHERE recall_id=? AND vehicle_id=? AND status IN ('reported','confirmed')", (recall_id, vehicle["id"])).fetchone()
        if duplicate: raise ApiError(409, "该车辆已有维修记录")
        scope = json.loads(recall["scope_json"])
        if not self._in_scope(vehicle, scope): raise ApiError(409, "车辆不在当前召回范围内")
        cross_border = dealer["country"] != vehicle["country"]
        if cross_border and not border_permit.strip(): raise ApiError(403, "跨境维修需要有效许可")
        part = self.conn.execute("SELECT * FROM parts WHERE recall_id=? AND dealer_id=? AND remedy_version=?", (recall_id, dealer_id, remedy_version)).fetchone()
        if not part or int(part["available"]) < 1: raise ApiError(409, "维修网点零件库存不足")
        with self.conn:
            cur = self.conn.execute("""INSERT INTO repairs(recall_id,vehicle_id,dealer_id,remedy_version,status,evidence_hash,evidence_consistent,
                                     cross_border,border_permit,idempotency_key,reported_by,reported_at)
                                     VALUES(?,?,?,?, 'reported',?,?,?,?,?,?,?)""",
                                    (recall_id, vehicle["id"], dealer_id, remedy_version, evidence_hash, int(evidence_consistent), int(cross_border), border_permit, idempotency_key, actor, now()))
            self.conn.execute("UPDATE parts SET available=available-1 WHERE id=? AND available>0", (part["id"],))
            self.store.audit(actor, "repair.report", "repair", cur.lastrowid, {"recall_id": recall_id, "vin": vehicle["vin"], "cross_border": cross_border})
        return dict(self._row("repairs", cur.lastrowid))

    def review_repair(self, actor: str | None, role: str | None, repair_id: int, decision: str, note: str = "") -> dict:
        actor = self._actor(actor, role, {"regulator"})
        if decision not in {"confirm", "flag", "reject"}: raise ApiError(400, "决定只能是 confirm、flag 或 reject")
        repair = self._row("repairs", repair_id)
        if repair["status"] != "reported": raise ApiError(409, "维修记录已经复核")
        if decision == "confirm":
            new_status = "confirmed" if repair["evidence_consistent"] else "flagged"
        elif decision == "flag":
            new_status = "flagged"
        else:
            new_status = "rejected"
        restock = decision in {"flag", "reject"}
        with self.conn:
            self.conn.execute("UPDATE repairs SET status=?,reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?", (new_status, actor, now(), note, repair_id))
            if restock:
                self.conn.execute("UPDATE parts SET available=available+1 WHERE recall_id=? AND dealer_id=? AND remedy_version=?",
                                  (repair["recall_id"], repair["dealer_id"], repair["remedy_version"]))
            if new_status == "confirmed":
                # 完成修复：车主自动退出失联名单
                self.contact_archive.close_for_repair(repair["recall_id"], repair["vehicle_id"])
            self.store.audit(actor, "repair.review", "repair", repair_id, {"decision": decision, "status": new_status, "note": note})
        return dict(self._row("repairs", repair_id))

    def reject_confirmed_repair(self, actor: str | None, role: str | None, repair_id: int, note: str = "") -> dict:
        """维修复核退回（含已确认记录）：已修数随之减少，零件退库，失联档案回到跟进。"""
        actor = self._actor(actor, role, {"regulator"})
        repair = self._row("repairs", repair_id)
        if repair["status"] not in {"confirmed", "reported"}: raise ApiError(409, "只有已报告或已确认记录可以退回")
        with self.conn:
            self.conn.execute("UPDATE repairs SET status='rejected',reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
                              (actor, now(), note, repair_id))
            self.conn.execute("UPDATE parts SET available=available+1 WHERE recall_id=? AND dealer_id=? AND remedy_version=?",
                              (repair["recall_id"], repair["dealer_id"], repair["remedy_version"]))
            self.contact_archive.reopen_for_return(repair["recall_id"], repair["vehicle_id"])
            self.store.audit(actor, "repair.reject", "repair", repair_id, {"previous": repair["status"], "note": note})
        return dict(self._row("repairs", repair_id))

    def mark_notification_sent(self, actor: str | None, role: str | None, recall_id: int, vin: str, scope_version: int | None = None) -> dict:
        """通知实际送达后出队，待通知数随之减少。"""
        actor = self._actor(actor, role, {"manufacturer", "dealer"})
        vehicle = self._row("vehicles", vin.upper(), "vin")
        self._reconcile_rosters(recall_id)
        query = "SELECT * FROM notifications WHERE recall_id=? AND vehicle_id=?"
        params: list[object] = [recall_id, vehicle["id"]]
        if scope_version is not None:
            query += " AND scope_version=?"; params.append(int(scope_version))
        query += " ORDER BY scope_version"
        rows = list(self.conn.execute(query, params))
        if not rows: raise ApiError(404, "没有对应的待发通知")
        stamp = now()
        with self.conn:
            for row in rows:
                if row["status"] == "queued":
                    self.conn.execute("UPDATE notifications SET status='sent', sent_at=? WHERE id=?", (stamp, row["id"]))
            self.store.audit(actor, "notification.sent", "recall", recall_id,
                             {"vin": vehicle["vin"], "ids": [r["id"] for r in rows]})
        updated = list(self.conn.execute("SELECT id,scope_version,status,sent_at FROM notifications WHERE id IN (%s)"
                                         % ",".join("?" * len(rows)), [r["id"] for r in rows]))
        return {"vin": vehicle["vin"], "notifications": [dict(r) for r in updated]}

    # ---- 失联名单 -------------------------------------------------------

    def open_unreachable(self, actor: str | None, role: str | None, recall_id: int, vin: str, note: str = "") -> dict:
        actor = self._actor(actor, role, {"manufacturer", "dealer", "regulator"})
        vehicle = self._row("vehicles", vin.upper(), "vin")
        recall = self._row("recalls", recall_id)
        self._reconcile_rosters(recall_id)
        if not self.scope_archive.get(recall_id, int(recall["scope_version"])):
            raise ApiError(409, "召回尚未发布，不能建立失联档案")
        in_any_version = self.conn.execute("SELECT 1 FROM scope_roster WHERE recall_id=? AND vehicle_id=? LIMIT 1",
                                           (recall_id, vehicle["id"])).fetchone()
        if not in_any_version:
            raise ApiError(409, "车辆不在召回范围内，不能建立失联档案")
        case = self.contact_archive.open_case(recall_id, vehicle["id"], actor, note)
        self.store.audit(actor, "contact.case_open", "unreachable_case", case["id"], {"vin": vehicle["vin"], "note": note})
        return self.contact_archive.case_detail(case)

    def record_contact(self, actor: str | None, role: str | None, case_id: int, result: str, channel: str = "phone", note: str = "") -> dict:
        actor = self._actor(actor, role, {"manufacturer", "dealer", "regulator"})
        if result not in {"failed", "reached"}: raise ApiError(400, "联系结果只能是 failed 或 reached")
        if not channel: raise ApiError(400, "联系渠道不能为空")
        try:
            case = self.contact_archive.record_attempt(case_id, result, channel, note, actor)
        except ValueError as exc:
            raise ApiError(409, str(exc)) from exc
        self.store.audit(actor, "contact.attempt", "unreachable_case", case_id, {"result": result, "channel": channel, "note": note})
        detail = self.contact_archive.case_detail(case)
        if result == "failed" and detail["status"] == "escalated":
            self.store.audit(actor, "contact.escalated", "unreachable_case", case_id, {"note": "三次联系未果，转监管跟进"})
        return detail

    def regulator_followups(self, actor: str | None, role: str | None, recall_id: int | None = None) -> dict:
        self._actor(actor, role, {"regulator", "manufacturer"})
        if recall_id is not None:
            self._row("recalls", recall_id)
        rows = self.contact_archive.followups(recall_id)
        cases = [{"case_id": r["id"], "recall_id": r["recall_id"], "campaign_code": r["campaign_code"],
                  "vin": r["vin"], "model": r["model"], "country": r["country"],
                  "attempts": r["attempts"], "escalated_at": r["escalated_at"]} for r in rows]
        return {"count": len(cases), "cases": cases, "generated_at": now()}

    def _reconcile_rosters(self, recall_id: int) -> None:
        """把发布后登记、但符合已发布版本范围的车辆补进对应版本名册。"""
        for archive in self.scope_archive.versions(recall_id):
            scope = json.loads(archive["scope_json"])
            vehicles = [row for row in self.conn.execute("SELECT * FROM vehicles ORDER BY id") if self._in_scope(row, scope)]
            added = self.scope_archive.reconcile(recall_id, archive["scope_version"], vehicles)
            if added:
                self.store.audit("system", "roster.reconcile", "recall", recall_id,
                                 {"scope_version": archive["scope_version"], "added": added})

    # ---- 成效复盘 -------------------------------------------------------

    def effectiveness(self, actor: str | None, role: str | None, recall_id: int, scope_version: int | None = None) -> dict:
        self._actor(actor, role, {"regulator", "manufacturer"})
        self._row("recalls", recall_id)
        self._reconcile_rosters(recall_id)
        if scope_version is not None:
            return self.stats.version_report(recall_id, int(scope_version))
        return self.stats.recall_report(recall_id)

    def _create_release_artifacts(self, recall_id: int, scope_version: int, actor: str) -> None:
        recall = self._row("recalls", recall_id); scope = json.loads(recall["scope_json"])
        vehicles = [row for row in self.conn.execute("SELECT * FROM vehicles ORDER BY id") if self._in_scope(row, scope)]
        stamp = now()
        with self.conn:
            for vehicle in vehicles:
                self.conn.execute("""INSERT OR IGNORE INTO notifications(recall_id,vehicle_id,scope_version,channel,status,created_at)
                                     VALUES(?,?,?, 'owner-notice','queued',?)""", (recall_id, vehicle["id"], scope_version, stamp))
            payload = {"campaign_code": recall["campaign_code"], "scope_version": scope_version, "scope": scope,
                       "remedy_version": recall["remedy_version"], "affected_count": len(vehicles)}
            self.conn.execute("INSERT OR IGNORE INTO regulatory_reports(recall_id,scope_version,payload_json,status,created_at) VALUES(?,?,?, 'queued',?)",
                              (recall_id, scope_version, j(payload), stamp))
        # 档案独立留档：版本名册按发布时点冻结，通知/上报队列不受影响
        self.scope_archive.release(recall_id, scope_version, scope, vehicles, actor, stamp)
        self.store.audit(actor, "recall.artifacts", "recall", recall_id, {"scope_version": scope_version, "affected_count": len(vehicles)})

    def unfinished(self, actor: str | None, role: str | None, recall_id: int) -> dict:
        self._actor(actor, role, {"manufacturer", "regulator"})
        recall = self._row("recalls", recall_id)
        scope = json.loads(recall["scope_json"])
        confirmed = {row["vehicle_id"] for row in self.conn.execute("SELECT vehicle_id FROM repairs WHERE recall_id=? AND status='confirmed'", (recall_id,))}
        current_year = datetime.now(timezone.utc).year
        items = []
        for vehicle in self.conn.execute("SELECT * FROM vehicles ORDER BY vin"):
            if self._in_scope(vehicle, scope) and vehicle["id"] not in confirmed:
                items.append({"vin": vehicle["vin"], "model": vehicle["model"], "model_year": vehicle["model_year"],
                              "country": vehicle["country"], "risk": "high" if current_year - int(vehicle["model_year"]) >= 8 else "normal"})
        return {"recall_id": recall_id, "scope_version": recall["scope_version"], "unfinished_count": len(items), "vehicles": items}

    @staticmethod
    def _in_scope(vehicle: sqlite3.Row, scope: dict) -> bool:
        return (vehicle["model"] in scope.get("models", []) and int(vehicle["model_year"]) in scope.get("model_years", [])
                and any(vehicle["vin"].startswith(prefix.upper()) for prefix in scope.get("vin_prefixes", []))
                and (vehicle["country"] in scope.get("countries", []) or vehicle["origin_country"] in scope.get("countries", [])))

    @staticmethod
    def _validate_scope(scope: dict) -> None:
        for key in ("models", "model_years", "vin_prefixes", "countries"):
            if not scope.get(key): raise ApiError(400, f"召回范围缺少 {key}")

    def _recall_dict(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "manufacturer": row["manufacturer"], "campaign_code": row["campaign_code"], "title": row["title"],
                "scope": json.loads(row["scope_json"]), "scope_version": row["scope_version"], "remedy": json.loads(row["remedy_json"]),
                "remedy_version": row["remedy_version"], "state": row["state"], "revision": row["revision"], "review_note": row["review_note"]}

    def recall_detail(self, recall_id: int) -> dict:
        result = self._recall_dict(self._row("recalls", recall_id))
        result["repairs"] = [dict(row) for row in self.conn.execute("SELECT * FROM repairs WHERE recall_id=? ORDER BY id", (recall_id,))]
        result["reports"] = [dict(row) for row in self.conn.execute("SELECT * FROM regulatory_reports WHERE recall_id=? ORDER BY scope_version", (recall_id,))]
        result["scope_archives"] = [{"scope_version": r["scope_version"], "archived_at": r["created_at"]}
                                    for r in self.scope_archive.versions(recall_id)]
        return result

    def state(self) -> dict:
        return {"dealers": [dict(row) for row in self.conn.execute("SELECT * FROM dealers ORDER BY id")],
                "vehicles": [dict(row) for row in self.conn.execute("SELECT * FROM vehicles ORDER BY id")],
                "recalls": [self._recall_dict(row) for row in self.conn.execute("SELECT * FROM recalls ORDER BY id DESC")],
                "unreachable_cases": [dict(row) for row in self.conn.execute("SELECT * FROM unreachable_cases ORDER BY id")],
                "contact_attempts": [dict(row) for row in self.conn.execute("SELECT * FROM contact_attempts ORDER BY id")],
                "audits": [dict(row) for row in self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 30")]}

    def seed(self) -> None:
        if not self.conn.execute("SELECT id FROM dealers LIMIT 1").fetchone():
            self.register_dealer("regulator-demo", "regulator", "D-CN", "演示中心", "CN")
        if not self.conn.execute("SELECT id FROM recalls LIMIT 1").fetchone():
            recall = self.create_recall("maker-demo", "manufacturer", "RC-2026-001", "制动管路检查",
                                        {"models": ["X1"], "model_years": [2018, 2019], "vin_prefixes": ["LX"], "countries": ["CN"]},
                                        {"version": 1, "description": "更换制动管", "deadline": "2027-03-31"})
            self.submit_recall("maker-demo", "manufacturer", recall["id"], recall["revision"])
