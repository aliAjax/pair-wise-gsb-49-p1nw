import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.ledger import cluster_pairs, layer_recovery, reinstatements_used


TERMS = {
    "peril": "typhoon", "hours_clause": 24.0,
    "attachment": 1000000.0, "limit_amount": 3000000.0,
    "cession_pct": 0.5, "reinstatement_pct": 0.1, "max_reinstatements": 2,
}

UW = Actor("u1", "underwriter")
CL = Actor("c1", "claims_officer")
FIN = Actor("f1", "finance")
ADMIN = Actor("root", "admin")
OUTSIDER = Actor("x", "outsider")


def at(day_hour: str) -> str:
    return "2026-09-%sT%s:00:00+00:00" % (day_hour[:2], day_hour[3:])


class HoursClusterTest(unittest.TestCase):
    def test_chain_clustering_by_gap(self):
        t = lambda s: datetime(2026, 9, int(s[0:2]), int(s[3:5]), tzinfo=timezone.utc)
        keys = ["A", "B", "C", "D"]
        pairs = [(k, t(v)) for k, v in zip(keys, ["01 10", "02 10", "03 10", "05 10"])]
        groups = cluster_pairs(pairs, 24.0)
        self.assertEqual(groups, [["A", "B", "C"], ["D"]])

    def test_gap_equal_to_hours_merges(self):
        t = lambda s: datetime(2026, 9, int(s[0:2]), int(s[3:5]), tzinfo=timezone.utc)
        groups = cluster_pairs([("A", t("01 10")), ("B", t("02 10"))], 24.0)
        self.assertEqual(len(groups), 1)
        groups = cluster_pairs([("A", t("01 10")), ("B", t("02 11"))], 24.0)
        self.assertEqual(len(groups), 2)

    def test_recovery_math(self):
        self.assertEqual(layer_recovery(2000000.0, TERMS), 500000.0)
        # 容量为层宽*成数=100万；首层不耗恢复，每满一层一次
        self.assertEqual(reinstatements_used(1000000.0, 1000000.0, 2), 0)
        self.assertEqual(reinstatements_used(1000001.0, 1000000.0, 2), 1)
        self.assertEqual(reinstatements_used(3000000.0, 1000000.0, 2), 2)


class LedgerServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "ledger.db")
        self.service = build_service(self.db)
        self.cid = self.service.create_contract(UW, "TREATY-A", TERMS)["id"]

    def tearDown(self):
        self.temp.cleanup()

    def register(self, number, day_hour, loss=2000000.0):
        return self.service.register_claim(CL, self.cid, {
            "claim_number": number, "occurred_at": at(day_hour), "loss_amount": loss})

    def approve(self, claim, loss=2000000.0):
        return self.service.ledger_action(CL, claim["id"], claim["version"], "approve_claim", {"approved_loss": loss})

    def settle(self, claim, ref="P"):
        return self.service.ledger_action(FIN, claim["id"], claim["version"], "settle_claim", {"payment_reference": ref})

    def active_occurrences(self):
        return [o for o in self.service.list_occurrences(CL, self.cid) if o["status"] == "active"]

    def get_claim(self, number):
        return next(c for c in self.service.list_claims(CL, self.cid) if c["claim_number"] == number)

    def test_hours_clause_separates_then_late_claim_bridges(self):
        a = self.register("A", "01 10")
        b = self.register("B", "02 11")  # 25小时 -> 两次事故
        self.assertEqual(len(self.active_occurrences()), 2)
        self.assertNotEqual(a["occurrence_id"], b["occurrence_id"])

        c = self.register("C", "01 22")  # 与A隔12h，与B隔13h，桥接
        active = self.active_occurrences()
        self.assertEqual(len(active), 1)
        members = sorted(x["claim_number"] for x in active[0]["claims"])
        self.assertEqual(members, ["A", "B", "C"])
        occ = active[0]
        self.assertEqual(occ["window_start"], at("01 10"))
        self.assertEqual(occ["window_end"], at("02 11"))
        a_after = self.get_claim("A")
        b_after = self.get_claim("B")
        c_after = self.get_claim("C")
        self.assertEqual(len({a_after["occurrence_id"], b_after["occurrence_id"], c_after["occurrence_id"]}), 1)

    def test_unsettled_claims_recomputed_after_reattribution(self):
        a = self.register("A", "01 10")
        b = self.register("B", "02 11")
        # B 先核定，占用其自身事故 50万
        b = self.approve(b)
        occ_b_before = b["occurrence_id"]
        self.assertEqual(next(o for o in self.service.list_occurrences(CL, self.cid) if o["id"] == occ_b_before)["occupancy"], 500000.0)
        # C 桥接后，B 作为未结算赔案按新事故重算
        self.register("C", "01 22")
        b_after = self.get_claim("B")
        self.assertNotEqual(b_after["occurrence_id"], occ_b_before)
        merged = self.active_occurrences()
        self.assertEqual(len(merged), 1)
        a_r = self.approve(self.get_claim("A"))
        c_r = self.approve(self.get_claim("C"))
        occ = self.active_occurrences()[0]
        self.assertEqual(occ["occupancy"], 1500000.0)
        self.assertEqual(occ["reinstatements_used"], 1)

    def test_settled_claim_keeps_basis_and_records_delta(self):
        self.register("A", "01 10")
        b = self.approve(self.register("B", "02 11"))
        b = self.settle(b, "PAY-B")
        occ_b_before = b["occurrence_id"]
        self.assertEqual(b["settled_recovery"], 500000.0)

        self.register("C", "01 22")
        b_after = self.get_claim("B")
        self.assertEqual(b_after["status"], "settled")
        self.assertEqual(b_after["settled_recovery"], 500000.0)  # 原结算依据保留
        self.assertNotEqual(b_after["occurrence_id"], occ_b_before)

        adjustments = self.service.list_adjustments(CL, self.cid)
        deltas = {(a["occurrence_id"], a["delta_amount"]) for a in adjustments}
        self.assertIn((occ_b_before, -500000.0), deltas)
        self.assertEqual(sum(a["delta_amount"] for a in adjustments), 0.0)

    def test_reject_bridge_splits_back_and_settled_returns_with_delta(self):
        self.register("A", "01 10")
        b = self.settle(self.approve(self.register("B", "02 11")), "PAY-B")
        self.register("C", "01 22")
        self.assertEqual(len(self.active_occurrences()), 1)

        c = self.get_claim("C")
        self.service.ledger_action(CL, c["id"], c["version"], "reject_claim", {"reject_reason": "不属于本合约"})
        active = self.active_occurrences()
        self.assertEqual(len(active), 2)
        groups = {o["window_start"]: sorted(x["claim_number"] for x in o["claims"] if x["status"] != "rejected") for o in active}
        self.assertEqual(groups[at("01 10")], ["A"])
        self.assertEqual(groups[at("02 11")], ["B"])
        b_after = self.get_claim("B")
        self.assertEqual(b_after["settled_recovery"], 500000.0)  # 已结算依据仍保留

    def test_concurrent_same_window_creates_one_occurrence(self):
        errors = []

        def reg(number, when):
            try:
                build_service(self.db).register_claim(CL, self.cid, {
                    "claim_number": number, "occurred_at": when, "loss_amount": 2000000.0})
            except Exception as exc:  # noqa: BLE001 - 测试需要收集任一失败
                errors.append((number, type(exc).__name__))

        threads = [
            threading.Thread(target=reg, args=("X", at("01 10"))),
            threading.Thread(target=reg, args=("Y", at("01 12"))),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        active = self.active_occurrences()
        self.assertEqual(len(active), 1)
        self.assertEqual(sorted(c["claim_number"] for c in active[0]["claims"]), ["X", "Y"])

    def test_merge_failure_then_reopen_recovers_consistent_ledger(self):
        self.register("A", "01 10")
        self.register("B", "02 11")
        self.service.repository.crash_in_next_apply = True
        with self.assertRaises(RuntimeError):
            self.register("C", "01 22")
        # 合并失败、重开前：仍是两场事故
        self.assertEqual(len(self.active_occurrences()), 2)
        # 模拟进程重开：构造时自动重放 pending
        reopened = build_service(self.db)
        active = [o for o in reopened.list_occurrences(CL, self.cid) if o["status"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(sorted(c["claim_number"] for c in active[0]["claims"]), ["A", "B", "C"])
        # 再放一次幂等
        self.assertEqual(reopened.reconcile(ADMIN), {"replayed_jobs": 0})

    def test_permissions_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_contract(CL, "TREATY-X", TERMS)
        with self.assertRaises(PermissionDenied):
            self.service.register_claim(UW, self.cid, {"claim_number": "Z", "occurred_at": at("01 10"), "loss_amount": 1})
        with self.assertRaises(PermissionDenied):
            self.service.reconcile(FIN)
        bad = dict(TERMS)
        bad["hours_clause"] = 0
        with self.assertRaises(ValidationError):
            self.service.create_contract(UW, "TREATY-BAD", bad)
        with self.assertRaises(ValidationError):
            self.service.register_claim(CL, self.cid, {"claim_number": "Z", "occurred_at": "not-a-time", "loss_amount": 1})

    def test_duplicate_claim_number_rejected(self):
        self.register("A", "01 10")
        with self.assertRaises(Conflict):
            self.register("A", "01 11")


class CapacityEnforcementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "cap.db"))
        self.cid = self.service.create_contract(UW, "CAP", {
            "peril": "typhoon", "hours_clause": 24.0, "attachment": 0.0, "limit_amount": 1000000.0,
            "cession_pct": 1.0, "reinstatement_pct": 0.0, "max_reinstatements": 1,
        })["id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_occurrence_capacity_exceeded(self):
        # 容量100万，含1次恢复上限200万：两笔核定损失各100万可行，第三笔触发冲突
        for n, day in [("A", "01 10"), ("B", "01 12")]:
            claim = self.service.register_claim(CL, self.cid, {"claim_number": n, "occurred_at": at(day), "loss_amount": 1000000.0})
            self.service.ledger_action(CL, claim["id"], claim["version"], "approve_claim", {"approved_loss": 1000000.0})
        c = self.service.register_claim(CL, self.cid, {"claim_number": "C", "occurred_at": at("01 14"), "loss_amount": 1.0})
        with self.assertRaises(Conflict):
            self.service.ledger_action(CL, c["id"], c["version"], "approve_claim", {"approved_loss": 1.0})
