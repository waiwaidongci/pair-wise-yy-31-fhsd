import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, RecallService, Store


class EffectivenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = RecallService(Store(Path(self.tmp.name) / "r.db"))
        self.eff = self.s.effectiveness
        self.dealer_cn = self.s.register_dealer("reg", "regulator", "D-CN", "中国中心", "CN")
        self.dealer_sg = self.s.register_dealer("reg", "regulator", "D-SG", "新加坡中心", "SG")

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def make_recall(self, countries=("CN", "SG")):
        scope = {"models": ["X"], "model_years": [2018], "vin_prefixes": ["LX"], "countries": list(countries)}
        r = self.s.create_recall("maker", "manufacturer", "RC-1", "制动检查", scope, {"version": 1, "description": "更换软管"})
        r = self.s.submit_recall("maker", "manufacturer", r["id"], r["revision"])
        return self.s.review_recall("reg", "regulator", r["id"], "publish", r["revision"], "同意发布")

    def repair(self, recall_id, vin, dealer, key):
        self.s.add_parts("maker", "manufacturer", recall_id, dealer["id"], 1, 1)
        report = self.s.report_repair("dealer", "dealer", recall_id, vin, dealer["id"], 1, "evidence", True, idempotency_key=key)
        return self.s.review_repair("reg", "regulator", report["id"], "confirm", "证据一致")

    def totals(self, recall_id, **kw):
        return self.eff.stats("reg", "regulator", recall_id, **kw)["current"]["totals"]

    def test_stats_grouped_by_country_model_and_transfer_counted_once(self):
        recall = self.make_recall()
        self.s.register_vehicle("maker", "manufacturer", "LX00001", "X", 2018, "CN", "张三")
        self.s.register_vehicle("maker", "manufacturer", "LX00002", "X", 2018, "CN", "李四")
        self.s.register_vehicle("maker", "manufacturer", "LX00003", "X", 2018, "SG", "Tan")
        self.s.transfer_vehicle("dealer", "dealer", "LX00001", "SG", "Wang")
        self.s.transfer_vehicle("dealer", "dealer", "LX00001", "CN", "张三")  # 多次转移只算一次
        self.repair(recall["id"], "LX00002", self.dealer_cn, "r-2")
        stats = self.eff.stats("reg", "regulator", recall["id"])
        self.assertEqual({"due": 3, "repaired": 1, "pending_notification": 3, "unreachable": 0, "overdue": 0}, stats["current"]["totals"])
        by_country = stats["current"]["by_country"]
        self.assertEqual(2, by_country["CN"]["due"])   # LX00001 转回 CN，只在 CN 计一次
        self.assertEqual(1, by_country["SG"]["due"])
        self.assertEqual(1, by_country["CN"]["repaired"])
        self.assertEqual(3, stats["current"]["by_model"]["X"]["due"])
        sent = self.s.notify_owners("maker", "manufacturer", recall["id"])
        self.assertEqual(3, sent["sent"])
        self.assertEqual(0, self.totals(recall["id"])["pending_notification"])

    def test_review_return_decreases_repaired(self):
        recall = self.make_recall(countries=("CN",))
        self.s.register_vehicle("maker", "manufacturer", "LX00001", "X", 2018, "CN", "张三")
        confirmed = self.repair(recall["id"], "LX00001", self.dealer_cn, "r-1")
        self.assertEqual(1, self.totals(recall["id"])["repaired"])
        returned = self.s.review_repair("reg", "regulator", confirmed["id"], "flag", "复核退回：证据复检不一致")
        self.assertEqual("flagged", returned["status"])
        self.assertEqual(0, self.totals(recall["id"])["repaired"])
        part = self.s.store.conn.execute("SELECT available FROM parts WHERE recall_id=?", (recall["id"],)).fetchone()
        self.assertEqual(1, part["available"])  # 退回后零件回补
        with self.assertRaises(ApiError):  # 已退回的记录不能重复复核
            self.s.review_repair("reg", "regulator", confirmed["id"], "flag", "again")

    def test_scope_change_archives_old_and_new_versions(self):
        recall = self.make_recall(countries=("CN",))
        self.s.register_vehicle("maker", "manufacturer", "LX00001", "X", 2018, "CN", "张三")
        self.s.register_vehicle("maker", "manufacturer", "LX00002", "X", 2018, "CN", "李四")
        self.repair(recall["id"], "LX00001", self.dealer_cn, "r-1")
        changed = self.s.change_scope("maker", "manufacturer", recall["id"],
                                      {"models": ["X"], "model_years": [2018], "vin_prefixes": ["LX"], "countries": ["CN", "SG"]}, recall["revision"])
        self.assertEqual(2, changed["scope_version"])
        self.s.register_vehicle("maker", "manufacturer", "LX00003", "X", 2018, "SG", "Tan")
        stats = self.eff.stats("reg", "regulator", recall["id"])
        self.assertEqual(2, stats["current"]["scope_version"])
        self.assertEqual(3, stats["current"]["totals"]["due"])
        self.assertEqual(1, len(stats["archived"]))
        old = stats["archived"][0]
        self.assertEqual(1, old["scope_version"])
        self.assertEqual({"due": 2, "repaired": 1, "pending_notification": 2, "unreachable": 0, "overdue": 0}, old["totals"])
        self.assertTrue(old["archived_at"])
        self.repair(recall["id"], "LX00003", self.dealer_sg, "r-3")  # 新版本继续修复
        again = self.eff.stats("reg", "regulator", recall["id"])
        self.assertEqual(2, again["current"]["totals"]["repaired"])
        self.assertEqual(1, again["archived"][0]["totals"]["repaired"])  # 旧版本账目保持留档不变

    def test_unreachable_list_lifecycle(self):
        recall = self.make_recall(countries=("CN",))
        self.s.register_vehicle("maker", "manufacturer", "LX00001", "X", 2018, "CN", "张三")
        first = self.eff.record_contact("dealer", "dealer", recall["id"], "LX00001", "unreachable", "电话空号")
        self.assertEqual("tracking", first["status"])
        self.eff.record_contact("dealer", "dealer", recall["id"], "LX00001", "unreachable", "短信未回")
        listed = self.eff.unreachable("reg", "regulator", recall["id"])
        self.assertEqual(1, listed["unreachable_count"])
        self.assertFalse(listed["vehicles"][0]["escalated"])
        self.assertEqual(2, len(listed["vehicles"][0]["contacts"]))  # 每次联系都记录
        third = self.eff.record_contact("maker", "manufacturer", recall["id"], "LX00001", "unreachable", "上门无人")
        self.assertEqual("escalated", third["status"])  # 三次未果转监管跟进
        listed = self.eff.unreachable("reg", "regulator", recall["id"])
        self.assertTrue(listed["vehicles"][0]["escalated"])
        self.assertTrue(listed["vehicles"][0]["escalated_at"])
        cleared = self.eff.record_contact("dealer", "dealer", recall["id"], "LX00001", "reached", "已约进店")
        self.assertEqual("cleared", cleared["status"])  # 联系成功退出名单
        self.assertEqual(0, self.eff.unreachable("reg", "regulator", recall["id"])["unreachable_count"])
        self.eff.record_contact("dealer", "dealer", recall["id"], "LX00001", "unreachable", "再次失联")
        self.assertEqual(1, self.totals(recall["id"])["unreachable"])  # 失联计入成效统计
        self.repair(recall["id"], "LX00001", self.dealer_cn, "r-1")
        self.assertEqual(0, self.eff.unreachable("reg", "regulator", recall["id"])["unreachable_count"])  # 完成修复退出名单
        self.assertEqual(0, self.totals(recall["id"])["unreachable"])
        with self.assertRaises(ApiError):  # 已修复车辆不能再登记联系
            self.eff.record_contact("dealer", "dealer", recall["id"], "LX00001", "unreachable", "")

    def test_contact_validation_and_permissions(self):
        recall = self.make_recall(countries=("CN",))
        self.s.register_vehicle("maker", "manufacturer", "LX00001", "X", 2018, "CN", "张三")
        self.s.register_vehicle("maker", "manufacturer", "ZZ00001", "Y", 2020, "CN", "王五")
        with self.assertRaises(ApiError):  # 监管不能代录联系记录
            self.eff.record_contact("reg", "regulator", recall["id"], "LX00001", "unreachable", "")
        with self.assertRaises(ApiError):  # 范围外车辆
            self.eff.record_contact("dealer", "dealer", recall["id"], "ZZ00001", "unreachable", "")
        with self.assertRaises(ApiError):  # 非法联系结果
            self.eff.record_contact("dealer", "dealer", recall["id"], "LX00001", "unknown", "")
        with self.assertRaises(ApiError):  # 网点不能查看成效复盘
            self.eff.stats("dealer", "dealer", recall["id"])

    def test_overdue_window(self):
        recall = self.make_recall(countries=("CN",))
        self.s.register_vehicle("maker", "manufacturer", "LX00001", "X", 2018, "CN", "张三")
        self.s.register_vehicle("maker", "manufacturer", "LX00002", "X", 2018, "CN", "李四")
        self.s.notify_owners("maker", "manufacturer", recall["id"])
        old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.s.store.conn.execute("UPDATE notifications SET created_at=? WHERE vehicle_id=(SELECT id FROM vehicles WHERE vin='LX00001')", (old,))
        self.assertEqual(1, self.totals(recall["id"])["overdue"])            # 默认 90 天口径
        self.assertEqual(0, self.totals(recall["id"], overdue_days=200)["overdue"])  # 口径可调
        self.repair(recall["id"], "LX00001", self.dealer_cn, "r-1")
        self.assertEqual(0, self.totals(recall["id"])["overdue"])            # 已修复不算超期


if __name__ == "__main__": unittest.main()
