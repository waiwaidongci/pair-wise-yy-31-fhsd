import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, RecallService, Store

SCOPE_V1 = {"models": ["X"], "model_years": [2018, 2019], "vin_prefixes": ["LX"], "countries": ["CN", "SG"]}


class EffectivenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = RecallService(Store(Path(self.tmp.name) / "r.db"))
        self.dealer_cn = self.s.register_dealer("reg", "regulator", "D-CN", "中国中心", "CN")
        self.dealer_sg = self.s.register_dealer("reg", "regulator", "D-SG", "新加坡中心", "SG")

    def tearDown(self):
        self.s.store.close(); self.tmp.cleanup()

    def make_recall(self, deadline="2020-01-01"):
        r = self.s.create_recall("maker", "manufacturer", "RC-1", "制动检查", SCOPE_V1,
                                 {"version": 1, "description": "更换软管", "deadline": deadline})
        r = self.s.submit_recall("maker", "manufacturer", r["id"], r["revision"])
        return self.s.review_recall("reg", "regulator", r["id"], "publish", r["revision"], "同意发布")

    def repair(self, recall, vin, dealer, key, permit=""):
        self.s.add_parts("maker", "manufacturer", recall["id"], dealer["id"], 1, 5)
        reported = self.s.report_repair("dealer", "dealer", recall["id"], vin, dealer["id"], 1, "ev-" + key, True, permit, key)
        return self.s.review_repair("reg", "regulator", reported["id"], "confirm", "证据一致")

    def test_dedup_transfer_breakdown_and_buckets(self):
        recall = self.make_recall()
        v1 = self.s.register_vehicle("maker", "manufacturer", "LX00001", "X", 2018, "CN", "张三")
        self.s.register_vehicle("maker", "manufacturer", "LX00002", "X", 2018, "CN", "李四")
        self.s.register_vehicle("maker", "manufacturer", "LX00003", "Y", 2018, "CN", "其他车型")  # 不在范围
        # 同一辆车跨境转移多次
        self.s.transfer_vehicle("dealer", "dealer", v1["vin"], "SG", "Wang")
        self.s.transfer_vehicle("dealer", "dealer", v1["vin"], "CN", "张三")
        self.s.transfer_vehicle("dealer", "dealer", v1["vin"], "SG", "Wang")

        report = self.s.effectiveness("reg", "regulator", recall["id"])
        totals = report["totals"]
        self.assertEqual(2, totals["affected"], "转移多次只算一次")
        self.assertEqual(0, totals["repaired"])
        self.assertEqual(2, totals["pending_notification"])
        self.assertEqual(2, totals["overdue"], "超期未修全部计入")
        self.assertEqual(0, totals["unreachable"])
        self.assertEqual(1, report["by_country"]["SG"]["affected"], "按当前所在国家归集")
        self.assertEqual(1, report["by_country"]["CN"]["affected"])
        self.assertNotIn("Y", report["by_model"])
        self.assertEqual(2, report["by_model"]["X"]["affected"])
        self.assertEqual(1, len(report["versions"]))

        # 通知送达后待通知减少
        self.s.mark_notification_sent("maker", "manufacturer", recall["id"], "LX00001")
        report = self.s.effectiveness("reg", "regulator", recall["id"])
        self.assertEqual(1, report["totals"]["pending_notification"])
        v1_ledger = report["versions"][0]["vehicles"][1]
        self.assertEqual(["LX00001", "LX00002"], [v["vin"] for v in report["versions"][0]["vehicles"]])

        # 完成一辆跨境修复
        self.repair(recall, "LX00001", self.dealer_sg, "repair-1", permit="BP-9")
        report = self.s.effectiveness("reg", "regulator", recall["id"])
        self.assertEqual(1, report["totals"]["repaired"])
        self.assertEqual(0.5, report["totals"]["repair_rate"])
        self.assertEqual(1, report["totals"]["overdue"], "已修的不再算超期")
        self.assertEqual(1, report["by_country"]["SG"]["repaired"])

    def test_repair_rejection_lowers_repaired_count(self):
        recall = self.make_recall()
        self.s.register_vehicle("maker", "manufacturer", "LX10001", "X", 2018, "CN", "赵六")
        confirmed = self.repair(recall, "LX10001", self.dealer_cn, "repair-9")
        self.assertEqual(1, self.s.effectiveness("reg", "regulator", recall["id"])["totals"]["repaired"])

        # 复核退回：已修数跟着减少，可以重新报告维修
        rejected = self.s.reject_confirmed_repair("reg", "regulator", confirmed["id"], "事后抽查证据不实")
        self.assertEqual("rejected", rejected["status"])
        report = self.s.effectiveness("reg", "regulator", recall["id"])
        self.assertEqual(0, report["totals"]["repaired"])
        self.assertEqual(1, report["totals"]["affected"])
        self.assertEqual(1, report["totals"]["overdue"])
        again = self.repair(recall, "LX10001", self.dealer_cn, "repair-10")
        self.assertEqual("confirmed", again["status"])
        self.assertEqual(1, self.s.effectiveness("reg", "regulator", recall["id"])["totals"]["repaired"])

        with self.assertRaises(ApiError):
            self.s.reject_confirmed_repair("maker", "manufacturer", again["id"])

    def test_scope_versions_keep_separate_ledgers(self):
        recall = self.make_recall(deadline="2099-01-01")
        self.s.register_vehicle("maker", "manufacturer", "LX20001", "X", 2018, "CN", "老车")
        self.s.register_vehicle("maker", "manufacturer", "LX20002", "X", 2019, "CN", "新车")
        v1 = self.s.effectiveness("reg", "regulator", recall["id"], 1)
        self.assertEqual(2, v1["totals"]["affected"])

        # 范围调整：新版本只保留 2019 款
        self.s.change_scope("maker", "manufacturer", recall["id"],
                            {"models": ["X"], "model_years": [2019], "vin_prefixes": ["LX"], "countries": ["CN", "SG"]},
                            recall["revision"])
        v1_again = self.s.effectiveness("reg", "regulator", recall["id"], 1)
        v2 = self.s.effectiveness("reg", "regulator", recall["id"], 2)
        self.assertEqual(2, v1_again["totals"]["affected"], "旧版本账留档不动")
        self.assertEqual(1, v2["totals"]["affected"])
        self.assertEqual(["LX20002"], [v["vin"] for v in v2["vehicles"]])

        # 召回层级跨版本去重：两辆车都在历史范围内，各算一次
        overall = self.s.effectiveness("maker", "manufacturer", recall["id"])
        self.assertEqual(2, overall["totals"]["affected"])
        self.assertEqual(2, len(overall["versions"]))

    def test_unreachable_three_attempts_then_exit_on_contact_or_repair(self):
        recall = self.make_recall(deadline="2099-01-01")
        self.s.register_vehicle("maker", "manufacturer", "LX30001", "X", 2018, "CN", "钱七")
        self.s.register_vehicle("maker", "manufacturer", "LX30002", "X", 2019, "CN", "孙八")

        case = self.s.open_unreachable("maker", "manufacturer", recall["id"], "LX30001", "信件退回")
        self.assertEqual("active", case["status"])
        case = self.s.record_contact("maker", "manufacturer", case["id"], "failed", "sms", "无回应")
        self.assertEqual("active", case["status"]); self.assertEqual(1, case["attempts"])
        case = self.s.record_contact("dealer", "dealer", case["id"], "failed", "phone", "关机")
        self.assertEqual("active", case["status"]); self.assertEqual(2, case["attempts"])
        report = self.s.effectiveness("reg", "regulator", recall["id"])
        self.assertEqual(1, report["totals"]["unreachable_active"])
        self.assertEqual(0, report["totals"]["regulator_followup"])

        # 第三次未果：转监管跟进
        case = self.s.record_contact("dealer", "dealer", case["id"], "failed", "visit", "上门无人")
        self.assertEqual("escalated", case["status"])
        self.assertEqual(3, case["attempts"])
        self.assertEqual(3, len(case["attempt_records"]))
        followups = self.s.regulator_followups("reg", "regulator", recall["id"])
        self.assertEqual(1, followups["count"])
        self.assertEqual("LX30001", followups["cases"][0]["vin"])
        report = self.s.effectiveness("reg", "regulator", recall["id"])
        self.assertEqual(0, report["totals"]["unreachable_active"])
        self.assertEqual(1, report["totals"]["regulator_followup"])

        # 完成修复即退出失联名单
        self.repair(recall, "LX30001", self.dealer_cn, "repair-31")
        report = self.s.effectiveness("reg", "regulator", recall["id"])
        self.assertEqual(0, report["totals"]["unreachable"])
        self.assertEqual(0, self.s.regulator_followups("reg", "regulator")["count"])

        # 另一辆车：第二次联系成功，立即退出
        case2 = self.s.open_unreachable("maker", "manufacturer", recall["id"], "LX30002")
        self.s.record_contact("maker", "manufacturer", case2["id"], "failed")
        case2 = self.s.record_contact("maker", "manufacturer", case2["id"], "reached", "phone", "车主回电")
        self.assertEqual("closed", case2["status"]); self.assertEqual("contacted", case2["close_reason"])
        report = self.s.effectiveness("reg", "regulator", recall["id"])
        self.assertEqual(0, report["totals"]["unreachable"])

        # 复核退回后，因修复退出的失联档案重新回到跟进
        confirmed = self.s.conn.execute("SELECT id FROM repairs WHERE status='confirmed'").fetchone()
        self.s.reject_confirmed_repair("reg", "regulator", confirmed["id"], "证据存疑")
        report = self.s.effectiveness("reg", "regulator", recall["id"])
        self.assertEqual(1, report["totals"]["regulator_followup"])

    def test_repair_review_reject_of_reported_restocks(self):
        recall = self.make_recall(deadline="2099-01-01")
        self.s.register_vehicle("maker", "manufacturer", "LX40001", "X", 2018, "CN", "周九")
        self.s.add_parts("maker", "manufacturer", recall["id"], self.dealer_cn["id"], 1, 1)
        reported = self.s.report_repair("dealer", "dealer", recall["id"], "LX40001", self.dealer_cn["id"], 1, "ev", True, idempotency_key="k1")
        out = self.s.review_repair("reg", "regulator", reported["id"], "reject", "缺材料")
        self.assertEqual("rejected", out["status"])
        self.assertEqual(0, self.s.effectiveness("reg", "regulator", recall["id"])["totals"]["repaired"])
        # 零件退回后可以重新报告
        again = self.s.report_repair("dealer", "dealer", recall["id"], "LX40001", self.dealer_cn["id"], 1, "ev2", True, idempotency_key="k2")
        self.assertEqual("reported", again["status"])


if __name__ == "__main__":
    unittest.main()
