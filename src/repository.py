"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .occurrences import gap_hours, normalize_time, parse_time, plan_assignment, reinstatements_used


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);

                CREATE TABLE IF NOT EXISTS cat_occurrences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    event_hours REAL NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    merged_into INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cat_occurrence_claims (
                    record_id INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
                    occurrence_id INTEGER NOT NULL REFERENCES cat_occurrences(id),
                    event_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_occurrence_seq
                    ON cat_occurrences(event_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_occurrence_event
                    ON cat_occurrences(event_id, active);
                CREATE INDEX IF NOT EXISTS idx_claim_event
                    ON cat_occurrence_claims(event_id, occurred_at);
                """
            )
        # 建表连接关闭后，修复可能残留的半成品事故合并
        self.reconcile_occurrences()

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def apply_claim_action(
        self,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        """串行写事务：执行赔案动作并重排巨灾事故台账。

        submit_claim 按发生时刻并入事故（必要时新建或桥接合并）；
        calculate/settle/reject 后，受影响事故的占用与恢复次数全部重算，
        未结算赔案改写事故依据，已结算赔案保留原依据，仅写差额审计。
        任何一步失败整体回滚，保证台账一致。
        """
        now = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            current = self._row(row)
            if int(current["version"]) != int(expected_version):
                raise Conflict("版本冲突，请刷新后重试")

            membership = connection.execute(
                "SELECT * FROM cat_occurrence_claims WHERE record_id=?", (record_id,)
            ).fetchone()
            touched: List[int] = [int(membership["occurrence_id"])] if membership is not None else []
            pending_frozen: Dict[int, float] = {}

            if action == "submit_claim":
                if membership is not None:
                    raise Conflict("该赔案已归属事故，不能重复报案")
                occurred_dt = parse_time(payload["occurred_at"])
                event_id = str(payload["claim_event_id"])
                event_hours = float(current["payload"].get("event_hours", 0))
                event_rows = connection.execute(
                    "SELECT * FROM cat_occurrences WHERE event_id=? AND active=1",
                    (event_id,),
                ).fetchall()
                events = []
                for event in event_rows:
                    events.append({
                        "id": int(event["id"]),
                        "started_dt": parse_time(event["started_at"]),
                        "ended_dt": parse_time(event["ended_at"]),
                    })
                plan = plan_assignment(events, occurred_dt, event_hours)
                started_text = normalize_time(occurred_dt)
                if plan["kind"] == "new":
                    ordinal_row = connection.execute(
                        "SELECT COALESCE(MAX(ordinal), 0) + 1 AS next_ordinal FROM cat_occurrences WHERE event_id=?",
                        (event_id,),
                    ).fetchone()
                    cursor = connection.execute(
                        "INSERT INTO cat_occurrences(event_id,ordinal,event_hours,started_at,ended_at,active,created_by,created_at)"
                        " VALUES(?,?,?,?,?,1,?,?)",
                        (event_id, int(ordinal_row["next_ordinal"]), event_hours, started_text, started_text, actor_id, now),
                    )
                    occurrence_id = int(cursor.lastrowid)
                elif plan["kind"] == "join":
                    occurrence_id = int(plan["occurrence_id"])
                else:
                    occurrence_id = int(plan["survivor_id"])
                    absorbed = [oid for oid in plan["occurrence_ids"] if oid != occurrence_id]
                    placeholders = ",".join("?" for _ in absorbed)
                    # 被吸收事故上的已结算赔案在合并后需补占用差额审计，
                    # 先记录其冻结依据（重算时其旧事故已查不到成员）
                    frozen_rows = connection.execute(
                        "SELECT c.record_id AS record_id, r.payload AS payload_json"
                        " FROM cat_occurrence_claims c JOIN records r ON r.id = c.record_id"
                        " WHERE c.occurrence_id IN (%s) AND c.active=1 AND r.state='settled'"
                        % placeholders,
                        absorbed,
                    ).fetchall()
                    for frozen in frozen_rows:
                        frozen_payload = json.loads(frozen["payload_json"])
                        pending_frozen[int(frozen["record_id"])] = float(
                            frozen_payload.get("settled_occurrence_recovery",
                                              frozen_payload.get("occurrence_recovery", 0.0))
                        )
                    connection.execute(
                        "UPDATE cat_occurrences SET active=0, merged_into=? WHERE id IN (%s)"
                        % placeholders,
                        [occurrence_id] + absorbed,
                    )
                    connection.execute(
                        "UPDATE cat_occurrence_claims SET occurrence_id=? WHERE occurrence_id IN (%s)"
                        % placeholders,
                        [occurrence_id] + absorbed,
                    )
                    touched.extend(absorbed)
                connection.execute(
                    "INSERT INTO cat_occurrence_claims(record_id,occurrence_id,event_id,occurred_at,active)"
                    " VALUES(?,?,?,?,1)",
                    (record_id, occurrence_id, event_id, started_text),
                )
                touched.append(occurrence_id)
            elif action == "reject" and membership is not None:
                connection.execute(
                    "UPDATE cat_occurrence_claims SET active=0 WHERE record_id=?", (record_id,)
                )

            # 先落主记录（新状态已生效）：占用/恢复字段随后按新事故重算并覆盖
            new_version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, new_version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )

            affected = self._recompute_occurrences(
                connection, sorted(set(touched)), actor_id, now, acting_id=record_id, pending_frozen=pending_frozen
            )

            if action == "settle":
                # 重算完成后冻结结算依据：占用与恢复次数取自本次重算结果
                result_row = connection.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
                settled_payload = json.loads(result_row["payload"])
                settled_payload["settled_occurrence_recovery"] = float(settled_payload.get("occurrence_recovery", 0.0))
                settled_payload["settled_reinstatements_used"] = int(settled_payload.get("reinstatements_used", 0))
                self._write_record(connection, record_id, settled_payload)

            action_details = dict(details)
            action_details["occurrences_recomputed"] = sorted(set(touched))
            action_details["affected_records"] = affected
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, new_version, json.dumps(action_details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            connection.close()
            raise
        connection.close()
        return self._row(result)

    @staticmethod
    def _write_record(connection: sqlite3.Connection, record_id: int, payload: Dict[str, Any]) -> None:
        connection.execute(
            "UPDATE records SET payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), record_id),
        )

    def _recompute_occurrences(
        self,
        connection: sqlite3.Connection,
        occurrence_ids: List[int],
        actor_id: str,
        now: str,
        acting_id: int,
        pending_frozen: Optional[Dict[int, float]] = None,
    ) -> Dict[str, float]:
        """按新事故重算占用与恢复次数。

        未结算赔案按新事故改写事故字段并升版本、记审计；已结算赔案保留
        原结算依据，只在占用发生变化时记一条差额审计；无版本变化。
        返回各事故占用总额（含已冻结的已结算摊回）。
        """
        totals: Dict[str, float] = {}
        pending_frozen = pending_frozen or {}
        for occurrence_id in occurrence_ids:
            members = connection.execute(
                "SELECT c.record_id AS record_id, c.occurred_at AS occurred_at, c.active AS claim_active,"
                " r.payload AS payload_json, r.state AS state"
                " FROM cat_occurrence_claims c JOIN records r ON r.id = c.record_id"
                " WHERE c.occurrence_id=? ORDER BY c.occurred_at, c.record_id",
                (occurrence_id,),
            ).fetchall()
            if members:
                starts = [m["occurred_at"] for m in members]
                connection.execute(
                    "UPDATE cat_occurrences SET started_at=?, ended_at=? WHERE id=?",
                    (min(starts), max(starts), occurrence_id),
                )
            # 第一遍：事故最终占用总额（含已结算的冻结摊回）
            final_total = 0.0
            for member in members:
                if int(member["claim_active"]) == 0 or member["state"] == "rejected":
                    continue
                p = json.loads(member["payload_json"])
                final_total = round(final_total + float(p.get("recoverable_amount", 0.0)), 2)

            # 第二遍：未结算赔案按顺序累计重算占用/恢复；已结算赔案只记差额
            cumulative = 0.0
            for member in members:
                if int(member["claim_active"]) == 0 or member["state"] == "rejected":
                    continue
                p = json.loads(member["payload_json"])
                width = float(p.get("layer_width", 0.0))
                amount = float(p.get("recoverable_amount", 0.0))
                used = reinstatements_used(cumulative, amount, width)
                cumulative = round(cumulative + amount, 2)
                rid = int(member["record_id"])
                if member["state"] == "settled":
                    frozen = pending_frozen.get(rid)
                    if frozen is None:
                        frozen = float(p.get("settled_occurrence_recovery", p.get("occurrence_recovery", 0.0)))
                    if abs(frozen - final_total) > 0.01:
                        connection.execute(
                            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at)"
                            " VALUES(?,?,?,?,?,?)",
                            (rid, "occurrence_delta", actor_id,
                             int(connection.execute("SELECT version FROM records WHERE id=?", (rid,)).fetchone()["version"]),
                             json.dumps({
                                 "summary": "事故归属调整，已结算赔案只记录占用差额",
                                 "occurrence_id": occurrence_id,
                                 "frozen_occurrence_recovery": round(frozen, 2),
                                 "new_occurrence_recovery": final_total,
                                 "recovery_delta": round(final_total - frozen, 2),
                             }, ensure_ascii=False, sort_keys=True), now),
                        )
                else:
                    new_payload = dict(p)
                    new_payload["occurrence_id"] = occurrence_id
                    new_payload["occurrence_recovery"] = cumulative
                    new_payload["reinstatements_used"] = used
                    version_row = connection.execute("SELECT version FROM records WHERE id=?", (rid,)).fetchone()
                    version = int(version_row["version"])
                    if rid != acting_id:
                        connection.execute(
                            "UPDATE records SET version=?, payload=? WHERE id=?",
                            (version + 1, json.dumps(new_payload, ensure_ascii=False, sort_keys=True), rid),
                        )
                        connection.execute(
                            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at)"
                            " VALUES(?,?,?,?,?,?)",
                            (rid, "occurrence_recomputed", actor_id, version + 1,
                             json.dumps({
                                 "summary": "事故归属调整，未结算赔案按新事故重算",
                                 "occurrence_id": occurrence_id,
                                 "occurrence_recovery": cumulative,
                                 "reinstatements_used": used,
                             }, ensure_ascii=False, sort_keys=True), now),
                        )
                    else:
                        # 主记录已在本事务写入payload，这里仅补齐重算后的事故字段
                        self._write_record(connection, rid, new_payload)
            totals[str(occurrence_id)] = final_total
        return totals

    def list_occurrences(self, event_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if event_id:
                rows = connection.execute(
                    "SELECT * FROM cat_occurrences WHERE event_id=? ORDER BY event_id, ordinal",
                    (event_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM cat_occurrences ORDER BY event_id, ordinal"
                ).fetchall()
            result = []
            for row in rows:
                claims = connection.execute(
                    "SELECT record_id, occurred_at, active FROM cat_occurrence_claims"
                    " WHERE occurrence_id=? ORDER BY occurred_at, record_id",
                    (row["id"],),
                ).fetchall()
                recovery = 0.0
                for claim in claims:
                    if int(claim["active"]) == 0:
                        continue
                    record = connection.execute("SELECT state, payload FROM records WHERE id=?", (claim["record_id"],)).fetchone()
                    if record is None or record["state"] == "rejected":
                        continue
                    recovery += float(json.loads(record["payload"]).get("recoverable_amount", 0.0))
                item = dict(row)
                item["claims"] = [dict(claim) for claim in claims]
                item["claim_count"] = len(claims)
                item["active_claim_count"] = sum(1 for claim in claims if int(claim["active"]) == 1)
                item["recovery_total"] = round(recovery, 2)
                result.append(item)
        return result

    def reconcile_occurrences(self, connection: Optional[sqlite3.Connection] = None) -> Dict[str, int]:
        """修复半成品事故合并，恢复一致台账。

        1) 归属事故已被并入他案的赔案改挂根事故；
        2) 仍挂有有效赔案的事故恢复有效；
        3) 同灾害下相互贴近（小时条款内）的事故补合并；
        4) 重算事故窗口及未结算赔案的占用与恢复次数（静默，不升版本）。

        幂等：正常事务提交后的台账执行此方法不产生任何改动。
        """
        own_connection = connection is None
        connection = connection or self._connect()
        repaired = {"repointed_claims": 0, "merged_events": 0, "recomputed_records": 0}
        try:
            # 1) 顺着 merged_into 链把赔案改挂到根事故
            rows = connection.execute(
                "SELECT c.rowid AS rowid, c.occurrence_id AS occurrence_id, o.merged_into AS merged_into"
                " FROM cat_occurrence_claims c JOIN cat_occurrences o ON o.id = c.occurrence_id"
            ).fetchall()
            for row in rows:
                root = int(row["occurrence_id"])
                seen = set()
                while root not in seen:
                    seen.add(root)
                    parent = connection.execute(
                        "SELECT merged_into FROM cat_occurrences WHERE id=?", (root,)
                    ).fetchone()
                    if parent is None or parent["merged_into"] is None:
                        break
                    root = int(parent["merged_into"])
                if root != int(row["occurrence_id"]):
                    connection.execute(
                        "UPDATE cat_occurrence_claims SET occurrence_id=? WHERE rowid=?",
                        (root, row["rowid"]),
                    )
                    repaired["repointed_claims"] += 1

            # 2) 挂有赔案的失效事故恢复有效
            connection.execute(
                "UPDATE cat_occurrences SET active=1, merged_into=NULL WHERE active=0"
                " AND id IN (SELECT DISTINCT occurrence_id FROM cat_occurrence_claims)"
            )

            # 3) 同灾害下仍在小时条款窗口内的事故补合并（链式扫描到稳定）
            while True:
                merged_any = False
                events = connection.execute(
                    "SELECT * FROM cat_occurrences WHERE active=1 ORDER BY event_id, id"
                ).fetchall()
                by_event: Dict[str, List[sqlite3.Row]] = {}
                for event in events:
                    by_event.setdefault(event["event_id"], []).append(event)
                for disaster in by_event.values():
                    for i, left in enumerate(disaster):
                        for right in disaster[i + 1:]:
                            gap = gap_hours(
                                parse_time(right["started_at"]),
                                parse_time(left["started_at"]),
                                parse_time(left["ended_at"]),
                            )
                            if gap <= min(float(left["event_hours"]), float(right["event_hours"])) + 1e-9:
                                survivor, absorbed = int(left["id"]), int(right["id"])
                                connection.execute(
                                    "UPDATE cat_occurrence_claims SET occurrence_id=? WHERE occurrence_id=?",
                                    (survivor, absorbed),
                                )
                                connection.execute(
                                    "UPDATE cat_occurrences SET active=0, merged_into=? WHERE id=?",
                                    (survivor, absorbed),
                                )
                                repaired["merged_events"] += 1
                                merged_any = True
                                break
                        if merged_any:
                            break
                    if merged_any:
                        break
                if not merged_any:
                    break

            # 4) 重算窗口与未结算赔案的事故字段（静默；已结算依据冻结，不动）
            events = connection.execute("SELECT id FROM cat_occurrences WHERE active=1").fetchall()
            for event in events:
                occurrence_id = int(event["id"])
                members = connection.execute(
                    "SELECT c.record_id AS record_id, c.occurred_at AS occurred_at, c.active AS claim_active,"
                    " r.payload AS payload_json, r.state AS state"
                    " FROM cat_occurrence_claims c JOIN records r ON r.id = c.record_id"
                    " WHERE c.occurrence_id=? ORDER BY c.occurred_at, c.record_id",
                    (occurrence_id,),
                ).fetchall()
                if not members:
                    continue
                connection.execute(
                    "UPDATE cat_occurrences SET started_at=?, ended_at=? WHERE id=?",
                    (min(m["occurred_at"] for m in members), max(m["occurred_at"] for m in members), occurrence_id),
                )
                cumulative = 0.0
                for member in members:
                    if int(member["claim_active"]) == 0 or member["state"] == "rejected":
                        continue
                    p = json.loads(member["payload_json"])
                    amount = float(p.get("recoverable_amount", 0.0))
                    width = float(p.get("layer_width", 0.0))
                    cumulative = round(cumulative + amount, 2)
                    # 已结算赔案保留原结算依据，只参与累计占用，不改写字段
                    if member["state"] == "settled":
                        continue
                    used = reinstatements_used(cumulative - amount, amount, width)
                    p["occurrence_id"] = occurrence_id
                    p["occurrence_recovery"] = cumulative
                    p["reinstatements_used"] = used
                    connection.execute(
                        "UPDATE records SET payload=? WHERE id=?",
                        (json.dumps(p, ensure_ascii=False, sort_keys=True), member["record_id"]),
                    )
                    repaired["recomputed_records"] += 1
            if own_connection:
                connection.commit()
        except Exception:
            if own_connection:
                connection.rollback()
            raise
        finally:
            if own_connection:
                connection.close()
        return repaired

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
