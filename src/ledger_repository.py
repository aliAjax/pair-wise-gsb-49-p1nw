"""巨灾事故台账的数据访问：两阶段合并、待处理任务与崩溃恢复。

结构变更（事故合并/拆分）分两个事务：
1. 意图事务：写入赔案/新事故骨架和 pending 任务并提交；
2. 应用事务：按任务计划重算归属、合并事故、记差额，成功后标记 applied。
若应用阶段失败，进程重开时 recover_pending_jobs 重放 pending 任务，
所有写入幂等，台账最终一致。
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .ledger import (
    _contribution,
    build_plan,
    layer_capacity,
    layer_recovery,
    reinstatement_premium,
    reinstatements_used,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    occ_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    merged_into INTEGER REFERENCES occurrences(id),
    merged_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_occ_active_window
ON occurrences(contract_id, window_start) WHERE status='active';
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    occurrence_id INTEGER REFERENCES occurrences(id),
    claim_number TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reported',
    occurred_at TEXT NOT NULL,
    loss_amount REAL NOT NULL,
    approved_loss REAL,
    recovery_amount REAL NOT NULL DEFAULT 0,
    settled_recovery REAL,
    payment_reference TEXT,
    reject_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(contract_id, claim_number)
);
CREATE TABLE IF NOT EXISTS occurrence_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES ledger_jobs(id),
    occurrence_id INTEGER NOT NULL REFERENCES occurrences(id),
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    delta_amount REAL NOT NULL,
    note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS occurrence_merges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES ledger_jobs(id),
    survivor_occurrence_id INTEGER NOT NULL REFERENCES occurrences(id),
    merged_occurrence_id INTEGER NOT NULL REFERENCES occurrences(id),
    bridge_claim_id INTEGER REFERENCES claims(id),
    created_at TEXT NOT NULL,
    UNIQUE(merged_occurrence_id)
);
CREATE TABLE IF NOT EXISTS ledger_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    trigger_claim_id INTEGER REFERENCES claims(id),
    status TEXT NOT NULL DEFAULT 'pending',
    plan TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER REFERENCES contracts(id),
    occurrence_id INTEGER REFERENCES occurrences(id),
    claim_id INTEGER REFERENCES claims(id),
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_occ_contract ON occurrences(contract_id, status);
CREATE INDEX IF NOT EXISTS idx_claims_contract ON claims(contract_id);
CREATE INDEX IF NOT EXISTS idx_claims_occ ON claims(occurrence_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON ledger_jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_events_contract ON ledger_events(contract_id, id);
"""


class LedgerRepositoryMixin:
    crash_in_next_apply = False

    # ---------- 基础读取 ----------
    @staticmethod
    def _contract_row(row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _claim_row(row) -> Dict[str, Any]:
        return dict(row)

    def create_contract_row(self, reference: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO contracts(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, "active", 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                contract_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO ledger_events(contract_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                    (contract_id, "contract_created", actor_id, json.dumps({"hours_clause": payload["hours_clause"]}, ensure_ascii=False), now),
                )
                row = connection.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise Conflict("合约reference已存在") from exc
            raise
        return self._contract_row(row)

    def _load_contract_locked(self, connection, contract_id: int) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        if row is None:
            raise NotFound("合约不存在")
        return self._contract_row(row)

    def get_contract_row(self, contract_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        if row is None:
            raise NotFound("合约不存在")
        return self._contract_row(row)

    def list_contract_rows(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM contracts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._contract_row(row) for row in rows]

    def _load_claims(self, connection, contract_id: int) -> List[Dict[str, Any]]:
        rows = connection.execute("SELECT * FROM claims WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()
        return [self._claim_row(row) for row in rows]

    def _load_occurrences(self, connection, contract_id: int) -> List[Dict[str, Any]]:
        rows = connection.execute("SELECT * FROM occurrences WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_claim_rows(self, contract_id: int) -> List[Dict[str, Any]]:
        self.get_contract_row(contract_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM claims WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()
        return [self._claim_row(row) for row in rows]

    def get_claim_row(self, claim_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None:
            raise NotFound("赔案不存在")
        return self._claim_row(row)

    # ---------- 待处理任务（应用阶段，幂等） ----------
    def _apply_pending_locked(self, connection, contract_id: int) -> int:
        applied = 0
        while True:
            job = connection.execute(
                "SELECT * FROM ledger_jobs WHERE contract_id=? AND status='pending' ORDER BY id LIMIT 1",
                (contract_id,),
            ).fetchone()
            if job is None:
                break
            if self.crash_in_next_apply:
                self.crash_in_next_apply = False
                raise RuntimeError("simulated merge failure")
            plan = json.loads(job["plan"])
            now = _now()
            for claim_id, occurrence_id in plan["assignments"].items():
                connection.execute(
                    "UPDATE claims SET occurrence_id=?, updated_at=? WHERE id=?",
                    (int(occurrence_id), now, int(claim_id)),
                )
            for occurrence_id, edges in plan.get("window_edges", {}).items():
                connection.execute(
                    "UPDATE occurrences SET window_end=?, updated_at=? WHERE id=? AND window_end<>?",
                    (edges[1], now, int(occurrence_id), edges[1]),
                )
            for merge in plan["merges"]:
                merged_id = int(merge["merged_id"])
                survivor_id = int(merge["survivor_id"])
                cursor = connection.execute(
                    "UPDATE occurrences SET status='merged', merged_into=?, merged_at=?, updated_at=? WHERE id=? AND status='active'",
                    (survivor_id, now, now, merged_id),
                )
                if cursor.rowcount > 0:
                    exists = connection.execute(
                        "SELECT 1 FROM occurrence_merges WHERE merged_occurrence_id=?", (merged_id,)
                    ).fetchone()
                    if exists is None:
                        connection.execute(
                            "INSERT INTO occurrence_merges(job_id,survivor_occurrence_id,merged_occurrence_id,bridge_claim_id,created_at) VALUES(?,?,?,?,?)",
                            (int(job["id"]), survivor_id, merged_id, merge.get("bridge_claim_id"), now),
                        )
                    connection.execute(
                        "INSERT INTO ledger_events(contract_id,occurrence_id,claim_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
                        (contract_id, survivor_id, merge.get("bridge_claim_id"), "occurrences_merged", job["created_by"],
                         json.dumps({"merged_occurrence_id": merged_id, "bridge_claim_id": merge.get("bridge_claim_id")}, ensure_ascii=False), now),
                    )
            for occurrence_id in plan["closures"]:
                connection.execute(
                    "UPDATE occurrences SET status='closed', updated_at=? WHERE id=? AND status='active'",
                    (now, int(occurrence_id)),
                )
            for adj in plan["adjustments"]:
                exists = connection.execute(
                    "SELECT 1 FROM occurrence_adjustments WHERE job_id=? AND claim_id=? AND occurrence_id=?",
                    (int(job["id"]), int(adj["claim_id"]), int(adj["occurrence_id"])),
                ).fetchone()
                if exists is None:
                    connection.execute(
                        "INSERT INTO occurrence_adjustments(job_id,occurrence_id,claim_id,delta_amount,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (int(job["id"]), int(adj["occurrence_id"]), int(adj["claim_id"]), float(adj["delta_amount"]), adj["note"], job["created_by"], now),
                    )
            connection.execute(
                "UPDATE ledger_jobs SET status='applied', applied_at=? WHERE id=?", (now, int(job["id"]))
            )
            applied += 1
        return applied

    def recover_pending_jobs(self) -> int:
        """进程重开后重放全部 pending 任务，恢复一致台账。"""
        total = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT DISTINCT contract_id FROM ledger_jobs WHERE status='pending' ORDER BY contract_id").fetchall()
            contract_ids = [int(row["contract_id"]) for row in rows]
        for contract_id in contract_ids:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                total += self._apply_pending_locked(connection, contract_id)
                connection.commit()
        return total

    # ---------- 结构计划 ----------
    def _insert_plan_job(self, connection, contract, claims, occurrences, actor_id, trigger_claim_id, now) -> Dict[str, Any]:
        plan = build_plan(contract=contract["payload"], claims=claims, occurrences=occurrences, trigger_claim_id=trigger_claim_id)
        structural = bool(plan["merges"] or plan["closures"] or plan["new_occurrences"] or plan["adjustments"])
        key_map: Dict[int, int] = {}
        if structural:
            next_no = int(connection.execute(
                "SELECT COALESCE(MAX(occ_no),0)+1 AS n FROM occurrences WHERE contract_id=?", (contract["id"],)
            ).fetchone()["n"])
            for new_occ in plan["new_occurrences"]:
                cursor = connection.execute(
                    "INSERT INTO occurrences(contract_id,occ_no,status,window_start,window_end,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (contract["id"], next_no, "active", new_occ["window_start"], new_occ["window_end"], actor_id, now, now),
                )
                key_map[int(new_occ["key"])] = int(cursor.lastrowid)
                next_no += 1

        def resolve(key: int) -> int:
            return key_map.get(int(key), int(key))

        resolved = {
            "assignments": {str(cid): resolve(target) for cid, target in plan["assignments"].items()},
            "merges": [{"survivor_id": resolve(m["survivor_id"]), "merged_id": resolve(m["merged_id"]),
                        "bridge_claim_id": m["bridge_claim_id"]} for m in plan["merges"]],
            "closures": [resolve(oid) for oid in plan["closures"]],
            "adjustments": [{"occurrence_id": resolve(a["occurrence_key"]), "claim_id": a["claim_id"],
                             "delta_amount": a["delta_amount"], "note": a["note"]} for a in plan["adjustments"]],
            "window_edges": {str(resolve(int(key))): edges for key, edges in plan["window_edges"].items()},
        }

        if structural:
            connection.execute(
                "INSERT INTO ledger_jobs(contract_id,trigger_claim_id,status,plan,created_by,created_at) VALUES(?,?, 'pending',?,?,?)",
                (contract["id"], trigger_claim_id, json.dumps(resolved, ensure_ascii=False, sort_keys=True), actor_id, now),
            )
        else:
            # 无结构变更：直接落定归属并延伸窗口
            for claim_id, target in resolved["assignments"].items():
                if trigger_claim_id is not None and int(claim_id) == int(trigger_claim_id):
                    edges = resolved["window_edges"][str(target)]
                    connection.execute(
                        "UPDATE occurrences SET window_end=?, updated_at=? WHERE id=? AND window_end<>?",
                        (edges[1], now, target, edges[1]),
                    )
        return {"structural": structural, "plan": resolved}

    # ---------- 赔案登记 ----------
    def register_claim_row(self, contract_id: int, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        structural = False
        claim_id: int
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._apply_pending_locked(connection, contract_id)
            contract = self._load_contract_locked(connection, contract_id)
            claims = self._load_claims(connection, contract_id)
            occurrences = self._load_occurrences(connection, contract_id)

            exists = connection.execute(
                "SELECT 1 FROM claims WHERE contract_id=? AND claim_number=?", (contract_id, payload["claim_number"])
            ).fetchone()
            if exists is not None:
                raise Conflict("赔案号在该合约下已存在")

            cursor = connection.execute(
                "INSERT INTO claims(contract_id,occurrence_id,claim_number,status,occurred_at,loss_amount,recovery_amount,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,0,?,?,?)",
                (contract_id, None, payload["claim_number"], "reported", payload["occurred_at"], payload["loss_amount"], actor_id, now, now),
            )
            claim_id = int(cursor.lastrowid)

            claims.append({"id": claim_id, "status": "reported", "occurred_at": payload["occurred_at"],
                           "loss_amount": payload["loss_amount"], "recovery_amount": 0.0,
                           "settled_recovery": None, "occurrence_id": None})
            outcome = self._insert_plan_job(connection, contract, claims, occurrences, actor_id, claim_id, now)
            structural = outcome["structural"]
            target_occ = int(outcome["plan"]["assignments"][str(claim_id)])
            connection.execute("UPDATE claims SET occurrence_id=? WHERE id=?", (target_occ, claim_id))
            connection.execute(
                "INSERT INTO ledger_events(contract_id,occurrence_id,claim_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
                (contract_id, target_occ, claim_id, "claim_registered", actor_id,
                 json.dumps({"claim_number": payload["claim_number"], "occurred_at": payload["occurred_at"],
                             "loss_amount": payload["loss_amount"], "pending_restructure": structural}, ensure_ascii=False), now),
            )
            connection.commit()

        # 应用阶段：独立事务，失败留下 pending 任务，由重开恢复重放
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._apply_pending_locked(connection, contract_id)
            connection.commit()

        return self.get_claim_row(claim_id)

    # ---------- 容量校验 ----------
    def _assert_occurrence_capacity(self, connection, occurrence_id: int, contract: Dict[str, Any]) -> None:
        terms = contract["payload"]
        rows = connection.execute("SELECT * FROM claims WHERE occurrence_id=? AND status<>'rejected'", (occurrence_id,)).fetchall()
        occupancy = round(sum(_contribution(dict(row)) for row in rows), 2)
        capacity = layer_capacity(terms)
        ceiling = capacity * (1 + int(terms["max_reinstatements"]))
        if occupancy > ceiling + 0.01:
            raise Conflict("事故累计摊回%s超过再保容量（含恢复）%s" % (occupancy, round(ceiling, 2)))

    def _claim_action_locked(self, connection, claim: Dict[str, Any], action: str, data: Dict[str, Any], actor_id: str, now: str) -> Dict[str, Any]:
        contract = self._load_contract_locked(connection, claim["contract_id"])
        terms = contract["payload"]
        status = claim["status"]
        new_version = int(claim["version"]) + 1
        details: Dict[str, Any] = {}
        new_status = status
        if action == "approve_claim":
            if status != "reported":
                raise Conflict("仅已报案未核定赔案可核定")
            approved_loss = float(data["approved_loss"])
            recovery = layer_recovery(approved_loss, terms)
            connection.execute(
                "UPDATE claims SET status='approved', approved_loss=?, recovery_amount=?, version=?, updated_at=? WHERE id=?",
                (approved_loss, recovery, new_version, now, claim["id"]),
            )
            new_status = "approved"
            details = {"approved_loss": approved_loss, "recovery_amount": recovery,
                       "reinstatement_premium": reinstatement_premium(recovery, terms)}
        elif action == "settle_claim":
            if status != "approved":
                raise Conflict("仅已核定赔案可结算")
            payment_reference = data["payment_reference"]
            if float(claim["recovery_amount"]) <= 0:
                raise Conflict("无可结算摊回")
            connection.execute(
                "UPDATE claims SET status='settled', settled_recovery=recovery_amount, payment_reference=?, version=?, updated_at=? WHERE id=?",
                (payment_reference, new_version, now, claim["id"]),
            )
            new_status = "settled"
            details = {"payment_reference": payment_reference, "settled_recovery": float(claim["recovery_amount"])}
        elif action == "reject_claim":
            if status not in {"reported", "approved"}:
                raise Conflict("当前赔案状态不允许拒绝")
            reason = data["reject_reason"]
            connection.execute(
                "UPDATE claims SET status='rejected', reject_reason=?, version=?, updated_at=? WHERE id=?",
                (reason, new_version, now, claim["id"]),
            )
            new_status = "rejected"
            details = {"reject_reason": reason}
        else:
            raise Conflict("未知赔案动作")
        connection.execute(
            "INSERT INTO ledger_events(contract_id,occurrence_id,claim_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
            (claim["contract_id"], claim["occurrence_id"], claim["id"], action, actor_id,
             json.dumps(details, ensure_ascii=False), now),
        )
        return {"contract": contract, "new_status": new_status, "details": details}

    def act_on_claim(self, claim_id: int, expected_version: int, action: str, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if claim is None:
                raise NotFound("赔案不存在")
            claim = dict(claim)
            if int(claim["version"]) != int(expected_version):
                raise Conflict("版本冲突，请刷新后重试")
            self._apply_pending_locked(connection, claim["contract_id"])
            claim = dict(connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone())
            result = self._claim_action_locked(connection, claim, action, data, actor_id, now)
            contract = result["contract"]
            if action == "reject_claim":
                connection.commit()
            else:
                self._assert_occurrence_capacity(connection, claim["occurrence_id"], contract)
                connection.commit()

        if action == "reject_claim":
            # 拒绝剔除赔案可能拆分事故：重建归属计划，两阶段执行
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._apply_pending_locked(connection, contract["id"])
                claims = self._load_claims(connection, contract["id"])
                occurrences = self._load_occurrences(connection, contract["id"])
                self._insert_plan_job(connection, contract, claims, occurrences, actor_id, claim_id, now)
                connection.commit()
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._apply_pending_locked(connection, contract["id"])
                connection.commit()

        return self.get_claim_row(claim_id)

    # ---------- 视图 ----------
    def list_occurrence_views(self, contract_id: int) -> List[Dict[str, Any]]:
        contract = self.get_contract_row(contract_id)
        capacity = layer_capacity(contract["payload"])
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM occurrences WHERE contract_id=? ORDER BY id", (contract_id,)
            ).fetchall()
            result = []
            for row in rows:
                occ = dict(row)
                claim_rows = connection.execute(
                    "SELECT * FROM claims WHERE occurrence_id=? ORDER BY occurred_at, id", (occ["id"],)
                ).fetchall()
                claims = [dict(r) for r in claim_rows]
                occupancy = round(sum(_contribution(c) for c in claims if c["status"] != "rejected"), 2)
                occ["claims"] = claims
                occ["capacity"] = capacity
                occ["occupancy"] = occupancy
                max_reinstatements = int(contract["payload"]["max_reinstatements"])
                occ["reinstatements_used"] = reinstatements_used(occupancy, capacity, max_reinstatements)
                occ["remaining_capacity"] = round(capacity * (1 + max_reinstatements) - occupancy, 2)
                result.append(occ)
        return result

    def list_adjustments(self, contract_id: Optional[int] = None) -> List[Dict[str, Any]]:
        if contract_id is not None:
            self.get_contract_row(contract_id)
        with self._connect() as connection:
            if contract_id:
                rows = connection.execute(
                    "SELECT a.* FROM occurrence_adjustments a JOIN occurrences o ON o.id=a.occurrence_id WHERE o.contract_id=? ORDER BY a.id",
                    (contract_id,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM occurrence_adjustments ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def ledger_timeline(self, contract_id: int) -> List[Dict[str, Any]]:
        self.get_contract_row(contract_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ledger_events WHERE contract_id=? ORDER BY id", (contract_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result
