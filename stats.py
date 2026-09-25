"""Statistics layer: read-only effectiveness review aggregation.

Numbers are always computed live from the archived scope-version rosters:
- each version keeps its own ledger (old and new scope versions are archived separately);
- a vehicle transferred across borders several times is still one roster row;
- confirmed repairs count as 已修, so a review rejection immediately lowers the number;
- the recall-level rollup unions every version, so a vehicle spanning versions counts once.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from archives import ContactArchive, ScopeArchive
from common import ApiError, now

BUCKETS = ("affected", "repaired", "pending_notification", "unreachable", "unreachable_active",
           "regulator_followup", "overdue")


def _empty_counts() -> dict:
    return {key: 0 for key in BUCKETS}


class EffectivenessStats:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.scope_archive = ScopeArchive(conn)
        self.contact_archive = ContactArchive(conn)

    def _vehicle(self, vehicle_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()

    def _deadline(self, recall_id: int) -> str | None:
        row = self.conn.execute("SELECT remedy_json FROM recalls WHERE id=?", (recall_id,)).fetchone()
        if not row:
            return None
        return json.loads(row["remedy_json"]).get("deadline")

    def _version_entries(self, recall_id: int, scope_version: int) -> list[dict]:
        confirmed = {r["vehicle_id"] for r in self.conn.execute(
            "SELECT vehicle_id FROM repairs WHERE recall_id=? AND status='confirmed'", (recall_id,))}
        pending = {r["vehicle_id"] for r in self.conn.execute(
            "SELECT vehicle_id FROM notifications WHERE recall_id=? AND scope_version=? AND status='queued'",
            (recall_id, scope_version))}
        cases = {r["vehicle_id"]: r for r in self.conn.execute(
            "SELECT * FROM unreachable_cases WHERE recall_id=? AND status IN ('active','escalated')", (recall_id,))}
        deadline = self._deadline(recall_id)
        today = datetime.now(timezone.utc).date().isoformat()
        entries = []
        for row in self.scope_archive.roster(recall_id, scope_version):
            vehicle = self._vehicle(row["vehicle_id"])
            repaired = vehicle["id"] in confirmed
            case = cases.get(vehicle["id"])
            overdue = bool(deadline and not repaired and deadline < today)
            entries.append({
                "vin": vehicle["vin"], "model": vehicle["model"], "model_year": vehicle["model_year"],
                "country": vehicle["country"], "repaired": repaired,
                "pending_notification": vehicle["id"] in pending,
                "unreachable": case["status"] if case else None,
                "contact_attempts": int(case["attempts"]) if case else 0,
                "overdue": overdue,
            })
        return entries

    @staticmethod
    def _tally(entries: list[dict]) -> dict:
        counts = _empty_counts()
        counts["affected"] = len(entries)
        for e in entries:
            if e["repaired"]:
                counts["repaired"] += 1
            if e["pending_notification"]:
                counts["pending_notification"] += 1
            if e["unreachable"] in ("active", "escalated"):
                counts["unreachable"] += 1
            if e["unreachable"] == "active":
                counts["unreachable_active"] += 1
            if e["unreachable"] == "escalated":
                counts["regulator_followup"] += 1
            if e["overdue"]:
                counts["overdue"] += 1
        counts["repair_rate"] = round(counts["repaired"] / counts["affected"], 4) if counts["affected"] else 0.0
        return counts

    @staticmethod
    def _breakdown(entries: list[dict], key: str) -> dict:
        groups: dict[str, list[dict]] = {}
        for e in entries:
            groups.setdefault(e[key], []).append(e)
        return {name: EffectivenessStats._tally(items) for name, items in sorted(groups.items())}

    def version_report(self, recall_id: int, scope_version: int) -> dict:
        archive = self.scope_archive.get(recall_id, scope_version)
        if not archive:
            raise ApiError(404, "该范围版本没有留档")
        entries = self._version_entries(recall_id, scope_version)
        return {"scope_version": scope_version, "archived_at": archive["created_at"],
                "scope": json.loads(archive["scope_json"]),
                "totals": self._tally(entries),
                "by_country": self._breakdown(entries, "country"),
                "by_model": self._breakdown(entries, "model"),
                "vehicles": entries,
                "generated_at": now()}

    def recall_report(self, recall_id: int) -> dict:
        versions = self.scope_archive.versions(recall_id)
        if not versions:
            raise ApiError(409, "召回尚未发布，暂无成效档案")
        merged: dict[int, dict] = {}
        version_reports = []
        for archive in versions:
            report = self.version_report(recall_id, archive["scope_version"])
            version_reports.append(report)
            for e in report["vehicles"]:
                cur = merged.get(e["vin"])
                if cur is None:
                    merged[e["vin"]] = {**e, "scope_versions": [report["scope_version"]]}
                else:
                    cur["scope_versions"].append(report["scope_version"])
                    # 车辆信息以当前登记为准；状态跨版本取并集
                    cur["repaired"] = cur["repaired"] or e["repaired"]
                    cur["pending_notification"] = cur["pending_notification"] or e["pending_notification"]
                    cur["overdue"] = cur["overdue"] or e["overdue"]
                    if e["unreachable"]:
                        cur["unreachable"] = e["unreachable"]
                        cur["contact_attempts"] = max(cur["contact_attempts"], e["contact_attempts"])
        entries = sorted(merged.values(), key=lambda e: e["vin"])
        row = self.conn.execute("SELECT campaign_code,title,scope_version,remedy_json FROM recalls WHERE id=?", (recall_id,)).fetchone()
        return {"recall_id": recall_id, "campaign_code": row["campaign_code"], "title": row["title"],
                "current_scope_version": row["scope_version"],
                "remedy_deadline": json.loads(row["remedy_json"]).get("deadline"),
                "totals": self._tally(entries),
                "by_country": self._breakdown(entries, "country"),
                "by_model": self._breakdown(entries, "model"),
                "versions": version_reports,
                "generated_at": now()}
