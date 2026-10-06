import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor
from src.occurrences import gap_hours, parse_time, plan_assignment, reinstatements_used

EVENT = "CAT-2026-09"
UW = Actor("creator", "underwriter")
CO = Actor("adjuster", "claims_officer")
FIN = Actor("treasurer", "finance")


def treaty_data(event_hours=24.0, loss_amount=0.0, cession_pct=0.4):
    return {
        "event_id": EVENT,
        "attachment": 1000000.0,
        "limit": 5000000.0,
        "cession_pct": cession_pct,
        "loss_amount": loss_amount,
        "reinstatement_pct": 0.15,
        "aggregate_prior": 0.0,
        "event_hours": event_hours,
    }


class OccurrenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)
        self.seq = 0

    def tearDown(self):
        self.temp.cleanup()

    def create_and_submit(self, occurred_at, reference=None, event_hours=24.0, cession_pct=0.4):
        self.seq += 1
        reference = reference or "RI-%03d" % self.seq
        record = self.service.create(UW, reference, treaty_data(event_hours=event_hours, cession_pct=cession_pct))
        record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-1"})
        record = self.service.act(
            CO,
            record["id"],
            record["version"],
            "submit_claim",
            {"claim_number": "CLM-%03d" % self.seq, "event_id": EVENT, "occurred_at": occurred_at},
        )
        return record

    def calculate(self, record, approved_loss):
        return self.service.act(
            CO, record["id"], record["version"], "calculate", {"approved_loss": approved_loss}
        )

    def settle(self, record, payment_reference):
        return self.service.act(
            FIN, record["id"], record["version"], "settle", {"payment_reference": payment_reference}
        )

    def active_events(self):
        return [item for item in self.service.occurrences(UW, event_id=EVENT) if item["active"]]

    def test_window_grouping_and_late_claim_bridge_merge(self):
        day1 = "2026-05-01T08:00:00+00:00"
        day2 = "2026-05-02T06:00:00+00:00"   # 距day1 22小时，同一事故
        day3 = "2026-05-03T08:00:00+00:00"   # 距day1 48小时、距day2 26小时，新事故
        self.create_and_submit(day1)
        self.create_and_submit(day2)
        self.create_and_submit(day3)
        events = self.active_events()
        self.assertEqual(len(events), 2)
        counts = sorted(event["active_claim_count"] for event in events)
        self.assertEqual(counts, [1, 2])

        # 晚到赔案：距第一起窗口端点24小时、距第二起26小时
        late = "2026-05-02T08:00:00+00:00"
        record = self.create_and_submit(late)
        events = self.active_events()
        self.assertEqual(len(events), 1, "晚到赔案桥接两起事故后应并成一次")
        self.assertEqual(events[0]["active_claim_count"], 4)
        self.assertEqual(record["payload"]["occurrence_id"], events[0]["id"])
        self.assertEqual(events[0]["started_at"], "2026-05-01T08:00:00+00:00")
        self.assertEqual(events[0]["ended_at"], "2026-05-03T08:00:00+00:00")

        # 台账中的失效事故只保留为合并痕迹
        all_events = self.service.occurrences(UW, event_id=EVENT)
        inactive = [event for event in all_events if not event["active"]]
        self.assertEqual(len(inactive), 1)
        self.assertEqual(inactive[0]["merged_into"], events[0]["id"])

    def test_capacity_and_reinstatements_recalculated_after_merge(self):
        # 100%分保、分层宽度4,000,000：损失11,000,000摊满一层4,000,000
        first = self.create_and_submit("2026-05-01T08:00:00+00:00", cession_pct=1.0)
        second = self.create_and_submit("2026-05-03T08:00:00+00:00", cession_pct=1.0)
        first = self.calculate(first, 11000000.0)
        second = self.calculate(second, 11000000.0)
        self.assertNotEqual(first["payload"]["occurrence_id"], second["payload"]["occurrence_id"])
        self.assertEqual(first["payload"]["occurrence_recovery"], 4000000.0)
        self.assertEqual(first["payload"]["reinstatements_used"], 0)
        self.assertEqual(second["payload"]["occurrence_recovery"], 4000000.0)
        self.assertEqual(second["payload"]["reinstatements_used"], 0)

        # 晚到赔案把两起事故接成一次：两笔合计8,000,000越过一层，第二笔用掉一次恢复
        bridge = self.create_and_submit("2026-05-02T08:00:00+00:00")
        first = self.service.get_record(UW, first["id"])
        second = self.service.get_record(UW, second["id"])
        occurrence_id = bridge["payload"]["occurrence_id"]
        self.assertEqual(first["payload"]["occurrence_id"], occurrence_id)
        self.assertEqual(second["payload"]["occurrence_id"], occurrence_id)
        self.assertEqual(second["payload"]["occurrence_recovery"], 8000000.0)
        self.assertEqual(second["payload"]["reinstatements_used"], 1)
        events = self.active_events()
        self.assertEqual(events[0]["recovery_total"], 8000000.0)

        timeline = self.service.timeline(UW, second["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("occurrence_recomputed", actions)

    def test_settled_claim_keeps_basis_and_records_delta(self):
        first = self.create_and_submit("2026-05-01T08:00:00+00:00", cession_pct=1.0)
        second = self.create_and_submit("2026-05-03T08:00:00+00:00", cession_pct=1.0)
        first = self.calculate(first, 11000000.0)
        second = self.calculate(second, 11000000.0)
        frozen_occurrence = first["payload"]["occurrence_id"]
        frozen_recovery = first["payload"]["occurrence_recovery"]
        first = self.settle(first, "PAY-1")
        self.assertEqual(first["payload"]["settled_occurrence_recovery"], 4000000.0)
        version_at_settlement = first["version"]

        # 晚到赔案合并两起事故，已结算赔案保留原依据，只记差额
        self.create_and_submit("2026-05-02T08:00:00+00:00")
        first_after = self.service.get_record(UW, first["id"])
        self.assertEqual(first_after["version"], version_at_settlement, "已结算赔案不应升版本")
        self.assertEqual(first_after["payload"]["occurrence_id"], frozen_occurrence)
        self.assertEqual(first_after["payload"]["settled_occurrence_recovery"], frozen_recovery)

        timeline = self.service.timeline(UW, first["id"])
        delta_events = [event for event in timeline if event["action"] == "occurrence_delta"]
        self.assertEqual(len(delta_events), 1)
        details = delta_events[0]["details"]
        self.assertEqual(details["frozen_occurrence_recovery"], 4000000.0)
        self.assertEqual(details["new_occurrence_recovery"], 8000000.0)
        self.assertEqual(details["recovery_delta"], 4000000.0)

    def test_concurrent_same_window_creates_single_occurrence(self):
        # 先独立建好两份合约并绑定
        def prepare(reference):
            service = build_service(self.db_path)
            record = service.create(UW, reference, treaty_data())
            record = service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-1"})
            return record["id"], record["version"]

        id_a, version_a = prepare("RI-101")
        id_b, version_b = prepare("RI-102")
        errors = []

        def submit(record_id, version, claim_no):
            service = build_service(self.db_path)
            try:
                service.act(
                    CO,
                    record_id,
                    version,
                    "submit_claim",
                    {"claim_number": claim_no, "event_id": EVENT, "occurred_at": "2026-05-01T08:00:00+00:00"},
                )
            except Exception as exc:  # pragma: no cover - 暴露线程内异常
                errors.append(exc)

        thread_a = threading.Thread(target=submit, args=(id_a, version_a, "CLM-101"))
        thread_b = threading.Thread(target=submit, args=(id_b, version_b, "CLM-102"))
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)
        self.assertFalse(thread_a.is_alive() or thread_b.is_alive())
        self.assertEqual(errors, [])

        events = self.active_events()
        self.assertEqual(len(events), 1, "同一窗口并发报案只建一次事故")
        self.assertEqual(events[0]["active_claim_count"], 2)

    def test_reconcile_repairs_half_finished_merge_on_reopen(self):
        first = self.create_and_submit("2026-05-01T08:00:00+00:00")
        second = self.create_and_submit("2026-05-03T08:00:00+00:00")
        survivor = first["payload"]["occurrence_id"]
        absorbed = second["payload"]["occurrence_id"]

        # 直接破坏台账，模拟“事故已标记并入但赔案改挂未完成”的半成品合并
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("UPDATE cat_occurrences SET active=0, merged_into=? WHERE id=?", (survivor, absorbed))
            connection.commit()
        finally:
            connection.close()

        # 重新打开服务即触发对账：赔案改挂根事故，两起事故收敛为一次
        service = build_service(self.db_path)
        events = [item for item in service.occurrences(UW, event_id=EVENT) if item["active"]]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], survivor)
        self.assertEqual(events[0]["active_claim_count"], 2)

        # 对账幂等：再跑一次不再产生结构修复
        report = service.repository.reconcile_occurrences()
        self.assertEqual(report["repointed_claims"], 0)
        self.assertEqual(report["merged_events"], 0)
        self.assertEqual(len([e for e in service.occurrences(UW, event_id=EVENT) if e["active"]]), 1)


class OccurrenceRulesTest(unittest.TestCase):
    def test_gap_and_plan(self):
        events = [
            {"id": 1, "started_dt": parse_time("2026-05-01T08:00:00+00:00"),
             "ended_dt": parse_time("2026-05-01T20:00:00+00:00")},
            {"id": 2, "started_dt": parse_time("2026-05-03T08:00:00+00:00"),
             "ended_dt": parse_time("2026-05-03T08:00:00+00:00")},
        ]
        point = parse_time("2026-05-02T08:00:00+00:00")
        self.assertEqual(gap_hours(point, events[0]["started_dt"], events[0]["ended_dt"]), 12.0)
        self.assertEqual(gap_hours(point, events[1]["started_dt"], events[1]["ended_dt"]), 24.0)
        self.assertEqual(plan_assignment(events, point, 24.0)["kind"], "merge")
        self.assertEqual(plan_assignment(events, point, 12.0)["kind"], "join")
        far = parse_time("2026-05-10T00:00:00+00:00")
        self.assertEqual(plan_assignment(events, far, 24.0)["kind"], "new")

    def test_reinstatements_used(self):
        width = 4000000.0
        self.assertEqual(reinstatements_used(0.0, 3600000.0, width), 0)
        self.assertEqual(reinstatements_used(3600000.0, 3600000.0, width), 1)
        # 单笔大额赔案连续跨越两档
        self.assertEqual(reinstatements_used(0.0, 9000000.0, width), 2)
