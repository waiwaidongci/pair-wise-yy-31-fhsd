"""Archive layer: scope-version ledgers and owner-contact (unreachable) records.

This layer only writes/reads historical records. Live effectiveness numbers are
computed by stats.EffectivenessStats; the HTTP boundary stays in app.py.
"""
from __future__ import annotations

import sqlite3

from common import j, now


class ScopeArchive:
    """范围版本档案：新旧版本的账各自留档，名册在发布时冻结。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def release(self, recall_id: int, scope_version: int, scope: dict, vehicles: list[sqlite3.Row], actor: str, stamp: str | None = None) -> int:
        stamp = stamp or now()
        with self.conn:
            self.conn.execute("""INSERT OR IGNORE INTO scope_archives(recall_id,scope_version,scope_json,created_by,created_at)
                                 VALUES(?,?,?,?,?)""", (recall_id, scope_version, j(scope), actor, stamp))
            for vehicle in vehicles:
                self.conn.execute("""INSERT OR IGNORE INTO scope_roster(recall_id,scope_version,vehicle_id,country_snapshot,model_snapshot,created_at)
                                     VALUES(?,?,?,?,?,?)""",
                                  (recall_id, scope_version, vehicle["id"], vehicle["country"], vehicle["model"], stamp))
            # 名册可能早已存在（通知先于档案生成），补齐发布时的国家/车型快照
            for vehicle in vehicles:
                self.conn.execute("""UPDATE scope_roster SET country_snapshot=?, model_snapshot=?
                                     WHERE recall_id=? AND scope_version=? AND vehicle_id=?""",
                                  (vehicle["country"], vehicle["model"], recall_id, scope_version, vehicle["id"]))
        return len(vehicles)

    def versions(self, recall_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM scope_archives WHERE recall_id=? ORDER BY scope_version", (recall_id,)))

    def get(self, recall_id: int, scope_version: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM scope_archives WHERE recall_id=? AND scope_version=?",
                                 (recall_id, scope_version)).fetchone()

    def roster(self, recall_id: int, scope_version: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM scope_roster WHERE recall_id=? AND scope_version=? ORDER BY id",
                                      (recall_id, scope_version)))

    def reconcile(self, recall_id: int, scope_version: int, in_scope_vehicles: list[sqlite3.Row], stamp: str | None = None) -> int:
        """发布后登记的车辆若属于该版本范围，补入名册（只增不改，已冻结快照不动）。"""
        stamp = stamp or now()
        existing = {r["vehicle_id"] for r in self.roster(recall_id, scope_version)}
        added = 0
        with self.conn:
            for vehicle in in_scope_vehicles:
                if vehicle["id"] in existing:
                    continue
                self.conn.execute("""INSERT OR IGNORE INTO scope_roster(recall_id,scope_version,vehicle_id,country_snapshot,model_snapshot,created_at)
                                     VALUES(?,?,?,?,?,?)""",
                                  (recall_id, scope_version, vehicle["id"], vehicle["country"], vehicle["model"], stamp))
                self.conn.execute("""INSERT OR IGNORE INTO notifications(recall_id,vehicle_id,scope_version,channel,status,created_at)
                                     VALUES(?,?,?, 'owner-notice','queued',?)""",
                                  (recall_id, vehicle["id"], scope_version, stamp))
                added += 1
        return added


class ContactArchive:
    """失联档案：记录每次联系，三次未果转监管，成功或修复完成即退出。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def open_case(self, recall_id: int, vehicle_id: int, actor: str, note: str = "", stamp: str | None = None) -> sqlite3.Row:
        stamp = stamp or now()
        existing = self.get_open_case(recall_id, vehicle_id)
        if existing:
            return existing
        with self.conn:
            cur = self.conn.execute("""INSERT INTO unreachable_cases(recall_id,vehicle_id,status,attempts,opened_by,opened_at)
                                       VALUES(?,?,'active',0,?,?)""", (recall_id, vehicle_id, actor, stamp))
        return self.get_case(cur.lastrowid)

    def get_case(self, case_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM unreachable_cases WHERE id=?", (case_id,)).fetchone()

    def get_open_case(self, recall_id: int, vehicle_id: int) -> sqlite3.Row | None:
        return self.conn.execute("""SELECT * FROM unreachable_cases WHERE recall_id=? AND vehicle_id=?
                                    AND status IN ('active','escalated')""", (recall_id, vehicle_id)).fetchone()

    def record_attempt(self, case_id: int, result: str, channel: str, note: str, actor: str, stamp: str | None = None) -> sqlite3.Row:
        stamp = stamp or now()
        case = self.get_case(case_id)
        if case["status"] == "closed":
            raise ValueError("该失联档案已关闭")
        attempt_no = int(case["attempts"]) + 1
        with self.conn:
            self.conn.execute("""INSERT INTO contact_attempts(case_id,attempt_no,channel,result,note,recorded_by,created_at)
                                 VALUES(?,?,?,?,?,?,?)""", (case_id, attempt_no, channel, result, note, actor, stamp))
            if result == "reached":
                self.conn.execute("""UPDATE unreachable_cases SET status='closed', attempts=?, closed_at=?, close_reason='contacted'
                                     WHERE id=?""", (attempt_no, stamp, case_id))
            elif attempt_no >= 3:
                # 连续三次联系未果，转监管跟进
                self.conn.execute("""UPDATE unreachable_cases SET status='escalated', attempts=?, escalated_at=COALESCE(escalated_at,?)
                                     WHERE id=?""", (attempt_no, stamp, case_id))
            else:
                self.conn.execute("UPDATE unreachable_cases SET attempts=? WHERE id=?", (attempt_no, case_id))
        return self.get_case(case_id)

    def close_for_repair(self, recall_id: int, vehicle_id: int, stamp: str | None = None) -> None:
        """完成修复后自动退出失联名单。"""
        stamp = stamp or now()
        with self.conn:
            self.conn.execute("""UPDATE unreachable_cases SET status='closed', closed_at=?, close_reason='repaired'
                                 WHERE recall_id=? AND vehicle_id=? AND status IN ('active','escalated')""",
                              (stamp, recall_id, vehicle_id))

    def reopen_for_return(self, recall_id: int, vehicle_id: int, stamp: str | None = None) -> None:
        """复核退回撤销修复，已退出的失联档案恢复到退出前的跟进状态。"""
        stamp = stamp or now()
        with self.conn:
            self.conn.execute("""UPDATE unreachable_cases
                                 SET status=CASE WHEN attempts>=3 THEN 'escalated' ELSE 'active' END,
                                     closed_at=NULL, close_reason=NULL,
                                     escalated_at=COALESCE(escalated_at, CASE WHEN attempts>=3 THEN ? END)
                                 WHERE recall_id=? AND vehicle_id=? AND status='closed' AND close_reason='repaired'""",
                              (stamp, recall_id, vehicle_id))

    def followups(self, recall_id: int | None = None) -> list[sqlite3.Row]:
        sql = """SELECT c.*, v.vin, v.model, v.country, r.campaign_code
                 FROM unreachable_cases c
                 JOIN vehicles v ON v.id=c.vehicle_id
                 JOIN recalls r ON r.id=c.recall_id
                 WHERE c.status='escalated'"""
        rows = self.conn.execute(sql + (" AND c.recall_id=?" if recall_id is not None else "") + " ORDER BY c.escalated_at, c.id",
                                 (recall_id,) if recall_id is not None else ()).fetchall()
        return list(rows)

    def case_detail(self, case: sqlite3.Row) -> dict:
        attempts = [dict(a) for a in self.conn.execute("SELECT attempt_no,channel,result,note,recorded_by,created_at FROM contact_attempts WHERE case_id=? ORDER BY attempt_no",
                                                        (case["id"],))]
        data = dict(case)
        data["attempt_records"] = attempts
        return data
