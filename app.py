#!/usr/bin/env python3
"""HTTP interface layer for the vehicle safety recall tracker.

Persistence lives in store.py, archival records in archives.py, read-only
effectiveness aggregation in stats.py and workflow rules in service.py.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from common import ApiError
from service import RecallService
from store import DB_PATH, Store


class Handler(BaseHTTPRequestHandler):
    service: RecallService

    def log_message(self, fmt: str, *args: object) -> None: sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        try: return json.loads(self.rfile.read(size)) if size else {}
        except json.JSONDecodeError as exc: raise ApiError(400, "JSON 请求体无效") from exc

    def _parts(self) -> list[str]: return [p for p in urlparse(self.path).path.strip("/").split("/") if p]

    def _query(self) -> dict[str, str]: return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def do_GET(self) -> None:
        try:
            p, q = self._parts(), self._query()
            actor, role = self.headers.get("X-Actor"), self.headers.get("X-Role")
            if p in (["health"], ["api", "health"]): out = {"status": "ok"}
            elif p == ["api", "state"]: out = self.service.state()
            elif len(p) == 3 and p[:2] == ["api", "recalls"]: out = self.service.recall_detail(int(p[2]))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "unfinished":
                out = self.service.unfinished(actor, role, int(p[2]))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "effectiveness":
                version = int(q["scope_version"]) if q.get("scope_version") else None
                out = self.service.effectiveness(actor, role, int(p[2]), version)
            elif p == ["api", "followups"]:
                out = self.service.regulator_followups(actor, role, None)
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "followups":
                out = self.service.regulator_followups(actor, role, int(p[2]))
            elif p == ["api", "unreachable"]:
                out = self.service.regulator_followups(actor, role, None)
            elif not p:
                page = (Path(__file__).parent / "static" / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page); return
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send(exc.status, {"error": exc.message})
        except Exception as exc: self._send(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            p, body = self._parts(), self._body(); actor, role = self.headers.get("X-Actor"), self.headers.get("X-Role")
            if p == ["api", "dealers"]: out = self.service.register_dealer(actor, role, body.get("code", ""), body.get("name", ""), body.get("country", ""))
            elif p == ["api", "vehicles"]: out = self.service.register_vehicle(actor, role, body.get("vin", ""), body.get("model", ""), int(body.get("model_year", 0)), body.get("country", ""), body.get("owner_name", ""))
            elif len(p) == 4 and p[:2] == ["api", "vehicles"] and p[3] == "transfer": out = self.service.transfer_vehicle(actor, role, p[2], body.get("country", ""), body.get("owner_name", ""))
            elif p == ["api", "recalls"]: out = self.service.create_recall(actor, role, body.get("campaign_code", ""), body.get("title", ""), body.get("scope", {}), body.get("remedy", {}))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "submit": out = self.service.submit_recall(actor, role, int(p[2]), int(body.get("expected_version", -1)))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "review": out = self.service.review_recall(actor, role, int(p[2]), body.get("decision", ""), int(body.get("expected_version", -1)), body.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "scope": out = self.service.change_scope(actor, role, int(p[2]), body.get("scope", {}), int(body.get("expected_version", -1)))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "parts": out = self.service.add_parts(actor, role, int(p[2]), int(body.get("dealer_id", 0)), int(body.get("remedy_version", 0)), int(body.get("quantity", 0)))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "notify": out = self.service.mark_notification_sent(actor, role, int(p[2]), body.get("vin", ""), body.get("scope_version"))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "unreachable": out = self.service.open_unreachable(actor, role, int(p[2]), body.get("vin", ""), body.get("note", ""))
            elif p == ["api", "repairs"]: out = self.service.report_repair(actor, role, int(body.get("recall_id", 0)), body.get("vin", ""), int(body.get("dealer_id", 0)), int(body.get("remedy_version", 0)), body.get("evidence_hash", ""), bool(body.get("evidence_consistent", True)), body.get("border_permit", ""), body.get("idempotency_key", ""))
            elif len(p) == 4 and p[:2] == ["api", "repairs"] and p[3] == "review": out = self.service.review_repair(actor, role, int(p[2]), body.get("decision", ""), body.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "repairs"] and p[3] == "reject": out = self.service.reject_confirmed_repair(actor, role, int(p[2]), body.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "unreachable"] and p[3] == "contact": out = self.service.record_contact(actor, role, int(p[2]), body.get("result", ""), body.get("channel", "phone"), body.get("note", ""))
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send(exc.status, {"error": exc.message})
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc: self._send(400, {"error": str(exc)})
        except Exception as exc: self._send(500, {"error": str(exc)})


def run(port: int, db_path: str, seed: bool) -> None:
    store = Store(db_path); service = RecallService(store)
    if seed: service.seed()
    Handler.service = service
    print(f"vehicle recall listening on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=8213); parser.add_argument("--db", default=str(DB_PATH)); parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    if args.init: Store(args.db).close()
    if args.seed or not args.init: run(args.port, args.db, args.seed)


if __name__ == "__main__": main()
