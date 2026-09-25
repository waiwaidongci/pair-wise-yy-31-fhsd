"""Persistence layer: SQLite schema, archival backfill and audit trail."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from common import j, now

DB_PATH = Path(__file__).with_name("data.db")


class Store:
    def __init__(self, path: str | Path = DB_PATH):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_schema()
        self.backfill_archives()

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS dealers (
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          country TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS vehicles (
          id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT UNIQUE NOT NULL, model TEXT NOT NULL,
          model_year INTEGER NOT NULL, country TEXT NOT NULL, origin_country TEXT NOT NULL, owner_name TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recalls (
          id INTEGER PRIMARY KEY AUTOINCREMENT, manufacturer TEXT NOT NULL, campaign_code TEXT UNIQUE NOT NULL,
          title TEXT NOT NULL, scope_json TEXT NOT NULL, remedy_version INTEGER NOT NULL,
          remedy_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('draft','submitted','published','returned')),
          scope_version INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
          review_note TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scope_changes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          scope_version INTEGER NOT NULL, scope_json TEXT NOT NULL, created_by TEXT NOT NULL,
          created_at TEXT NOT NULL, UNIQUE(recall_id,scope_version)
        );
        CREATE TABLE IF NOT EXISTS parts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          dealer_id INTEGER NOT NULL REFERENCES dealers(id), remedy_version INTEGER NOT NULL,
          available INTEGER NOT NULL CHECK(available>=0), UNIQUE(recall_id,dealer_id,remedy_version)
        );
        CREATE TABLE IF NOT EXISTS repairs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id), dealer_id INTEGER NOT NULL REFERENCES dealers(id),
          remedy_version INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('reported','confirmed','flagged','rejected')),
          evidence_hash TEXT NOT NULL, evidence_consistent INTEGER NOT NULL, cross_border INTEGER NOT NULL DEFAULT 0,
          border_permit TEXT, idempotency_key TEXT NOT NULL, reported_by TEXT NOT NULL,
          reported_at TEXT NOT NULL, reviewed_by TEXT, reviewed_at TEXT, review_note TEXT,
          UNIQUE(recall_id,vehicle_id,idempotency_key)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_repair ON repairs(recall_id,vehicle_id) WHERE status='confirmed';
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id), scope_version INTEGER NOT NULL,
          channel TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','sent')),
          created_at TEXT NOT NULL, sent_at TEXT,
          UNIQUE(recall_id,vehicle_id,scope_version)
        );
        CREATE TABLE IF NOT EXISTS regulatory_reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          scope_version INTEGER NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          created_at TEXT NOT NULL, UNIQUE(recall_id,scope_version)
        );
        -- 成效复盘档案：每个召回的每个范围版本一份留档（快照）
        CREATE TABLE IF NOT EXISTS scope_archives (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          scope_version INTEGER NOT NULL, scope_json TEXT NOT NULL, created_by TEXT NOT NULL,
          created_at TEXT NOT NULL, UNIQUE(recall_id,scope_version)
        );
        -- 范围版本车辆名册：版本发布时应修车辆快照，跨境转移多次也只入册一次
        CREATE TABLE IF NOT EXISTS scope_roster (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          scope_version INTEGER NOT NULL, vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
          country_snapshot TEXT NOT NULL, model_snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(recall_id,scope_version,vehicle_id)
        );
        -- 失联名单：每车每召回最多一条未关闭档案
        CREATE TABLE IF NOT EXISTS unreachable_cases (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
          status TEXT NOT NULL CHECK(status IN ('active','escalated','closed')),
          attempts INTEGER NOT NULL DEFAULT 0, opened_by TEXT NOT NULL, opened_at TEXT NOT NULL,
          escalated_at TEXT, closed_at TEXT, close_reason TEXT,
          UNIQUE(recall_id,vehicle_id)
        );
        -- 每次联系都留痕；连续三次未果才转监管跟进
        CREATE TABLE IF NOT EXISTS contact_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES unreachable_cases(id),
          attempt_no INTEGER NOT NULL, channel TEXT NOT NULL, result TEXT NOT NULL CHECK(result IN ('failed','reached')),
          note TEXT NOT NULL DEFAULT '', recorded_by TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(case_id,attempt_no)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
          entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL
        );
        """)
        self.conn.commit()

    def backfill_archives(self) -> None:
        """旧库升级：用监管上报载荷和通知队列补齐版本留档与名册。"""
        pending = self.conn.execute("""SELECT r.id AS recall_id, rep.scope_version, rep.payload_json, rep.created_at
                                       FROM regulatory_reports rep
                                       JOIN recalls r ON r.id = rep.recall_id
                                       WHERE NOT EXISTS (SELECT 1 FROM scope_archives a
                                                         WHERE a.recall_id=rep.recall_id AND a.scope_version=rep.scope_version)""").fetchall()
        if not pending:
            return
        with self.conn:
            for row in pending:
                payload = json.loads(row["payload_json"])
                self.conn.execute("""INSERT OR IGNORE INTO scope_archives(recall_id,scope_version,scope_json,created_by,created_at)
                                     VALUES(?,?,?, 'system-backfill',?)""",
                                  (row["recall_id"], row["scope_version"], j(payload.get("scope", {})), row["created_at"]))
                for n in self.conn.execute("SELECT vehicle_id,created_at FROM notifications WHERE recall_id=? AND scope_version=?",
                                           (row["recall_id"], row["scope_version"])):
                    v = self.conn.execute("SELECT country,model FROM vehicles WHERE id=?", (n["vehicle_id"],)).fetchone()
                    if v:
                        self.conn.execute("""INSERT OR IGNORE INTO scope_roster(recall_id,scope_version,vehicle_id,country_snapshot,model_snapshot,created_at)
                                             VALUES(?,?,?,?,?,?)""",
                                          (row["recall_id"], row["scope_version"], n["vehicle_id"], v["country"], v["model"], n["created_at"]))

    def audit(self, actor: str, action: str, entity_type: str, entity_id: object, details: dict) -> None:
        self.conn.execute("INSERT INTO audit_log(at,actor,action,entity_type,entity_id,details_json) VALUES(?,?,?,?,?,?)",
                          (now(), actor, action, entity_type, str(entity_id), j(details)))

    def close(self) -> None:
        self.conn.close()
