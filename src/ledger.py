"""巨灾事故台账：小时条款聚合、容量占用与恢复次数计算。

小时条款认定规则：同一合约下，相邻两笔有效赔案的发生时刻间隔不超过合约
约定小时数即属于同一次巨灾事故（链式聚合）。晚到赔案若把两次事故之间的
时间缺口补上，则两次事故合并为一次。
"""
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, ValidationError, integer, number, text


EPS = 0.01

LEDGER_CREATE_ROLES = {"underwriter", "admin"}
LEDGER_ACTION_ROLES = {
    "register_claim": {"claims_officer", "admin"},
    "approve_claim": {"claims_officer", "admin"},
    "reject_claim": {"claims_officer", "finance", "admin"},
    "settle_claim": {"finance", "admin"},
    "reconcile": {"admin"},
}
CLAIM_ACTIONS = {"approve_claim", "settle_claim", "reject_claim"}


def role_can_create_contract(role: str) -> bool:
    return role == "admin" or role in LEDGER_CREATE_ROLES


def role_can_ledger_action(role: str, action: str) -> bool:
    return role == "admin" or role in LEDGER_ACTION_ROLES.get(action, set())


def parse_time(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("occurred_at不能为空")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("occurred_at必须是ISO8601时间") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def validate_contract_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload or {})
    peril = text(p, "peril")
    hours = number(p, "hours_clause", 0.0)
    if hours <= 0:
        raise ValidationError("hours_clause必须大于0")
    attachment = number(p, "attachment", 0)
    limit = number(p, "limit_amount", 0)
    if limit <= attachment:
        raise ValidationError("赔款限额必须高于起赔点")
    cession = number(p, "cession_pct", 0, 1)
    reinstatement_pct = number(p, "reinstatement_pct", 0, 1)
    max_reinstatements = integer(p, "max_reinstatements", 0)
    return {
        "peril": peril,
        "hours_clause": hours,
        "attachment": attachment,
        "limit_amount": limit,
        "cession_pct": cession,
        "reinstatement_pct": reinstatement_pct,
        "max_reinstatements": max_reinstatements,
    }


def validate_claim_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload or {})
    claim_number = text(p, "claim_number")
    occurred_at = to_iso(parse_time(p.get("occurred_at", "")))
    loss_amount = number(p, "loss_amount", 0)
    return {"claim_number": claim_number, "occurred_at": occurred_at, "loss_amount": loss_amount}


def layer_width(contract: Dict[str, Any]) -> float:
    return round(float(contract["limit_amount"]) - float(contract["attachment"]), 2)


def layer_capacity(contract: Dict[str, Any]) -> float:
    return round(layer_width(contract) * float(contract["cession_pct"]), 2)


def layer_recovery(loss_amount: float, contract: Dict[str, Any]) -> float:
    width = layer_width(contract)
    retained = max(0.0, float(loss_amount) - float(contract["attachment"]))
    return round(min(retained, width) * float(contract["cession_pct"]), 2)


def reinstatement_premium(recovery_amount: float, contract: Dict[str, Any]) -> float:
    return round(recovery_amount * float(contract["reinstatement_pct"]), 2)


def reinstatements_used(occupancy: float, capacity: float, max_reinstatements: int) -> int:
    """事故占用达到几个分层限额：首层不耗恢复，每多满一层计一次恢复。"""
    if capacity <= 0 or occupancy <= EPS:
        return 0
    layers = ceil((occupancy - 1e-9) / capacity)
    return min(int(max_reinstatements), max(0, layers - 1))


def cluster_pairs(pairs: List[Tuple[Any, datetime]], hours: float) -> List[List[Any]]:
    """按相邻时间间隔链式聚类，间隔不超过hours小时归为同组。"""
    ordered = sorted(pairs, key=lambda item: (item[1], item[0]))
    clusters: List[List[Any]] = []
    current: List[Any] = []
    last_time: Optional[datetime] = None
    for key, when in ordered:
        if not current or (when - last_time) <= timedelta(hours=hours):
            current.append(key)
        else:
            clusters.append(current)
            current = [key]
        last_time = when
    if current:
        clusters.append(current)
    return clusters


def _contribution(claim: Dict[str, Any]) -> float:
    if claim["status"] == "settled":
        return round(float(claim.get("settled_recovery") or 0.0), 2)
    return round(float(claim.get("recovery_amount") or 0.0), 2)


def build_plan(
    *,
    contract: Dict[str, Any],
    claims: List[Dict[str, Any]],
    occurrences: List[Dict[str, Any]],
    trigger_claim_id: Optional[int] = None,
) -> Dict[str, Any]:
    """依据当前赔案发生时刻重建事故归属，返回可持久化的台账变更计划。

    返回内容：
    - assignments: 赔案id -> 目标事故（负数表示待建的新事故）
    - new_occurrences: 新事故的临时编号与窗口起止
    - merges: 被并入的事故及桥接赔案
    - closures: 已无有效赔案的事故
    - adjustments: 已结算赔案改归属时保留原依据、只记差额
    """
    hours = float(contract["hours_clause"])
    capacity = layer_capacity(contract)
    max_reinstatements = int(contract["max_reinstatements"])

    effective = [c for c in claims if c["status"] != "rejected"]
    times = {c["id"]: parse_time(c["occurred_at"]) for c in effective}
    old_owner = {c["id"]: c.get("occurrence_id") for c in effective}
    active_ids = {o["id"] for o in occurrences if o["status"] == "active"}

    pre_occupancy: Dict[int, float] = {}
    for c in effective:
        owner = old_owner.get(c["id"])
        if owner:
            pre_occupancy[owner] = pre_occupancy.get(owner, 0.0) + _contribution(c)

    groups = cluster_pairs([(c["id"], times[c["id"]]) for c in effective], hours)

    assignments: Dict[int, int] = {}
    new_occurrences: List[Dict[str, Any]] = []
    merges: List[Dict[str, Any]] = []
    closures: List[int] = []
    window_edges: Dict[int, Tuple[datetime, datetime]] = {}

    candidate_pool = set(active_ids)
    next_new_key = -1
    for group in groups:
        existing = [oid for oid in (old_owner.get(cid) for cid in group)
                    if oid and oid in candidate_pool]
        # 同一事故只能承接一个聚类：按事故id（即创建先后）稳定分配
        seen = set()
        existing = [oid for oid in existing if not (oid in seen or seen.add(oid))]
        if existing:
            target = min(existing)
            candidate_pool.discard(target)
            for oid in existing:
                if oid == target:
                    continue
                candidate_pool.discard(oid)
                bridge = trigger_claim_id if trigger_claim_id in group else None
                merges.append({"survivor_id": target, "merged_id": oid, "bridge_claim_id": bridge})
        else:
            target = next_new_key
            next_new_key -= 1
            members_times = [times[cid] for cid in group]
            new_occurrences.append({
                "key": target,
                "window_start": to_iso(min(members_times)),
                "window_end": to_iso(max(members_times)),
            })
        for cid in group:
            assignments[cid] = target
        members_times = [times[cid] for cid in group]
        window_edges[target] = (min(members_times), max(members_times))

    # 没有任何有效赔案、也没有被合并的旧事故关闭
    merged_ids = {m["merged_id"] for m in merges}
    targets = set(assignments.values())
    for oid in active_ids:
        if oid not in targets and oid not in merged_ids:
            closures.append(oid)

    post_occupancy: Dict[int, float] = {}
    for c in effective:
        target = assignments[c["id"]]
        post_occupancy[target] = post_occupancy.get(target, 0.0) + _contribution(c)

    ceiling = capacity * (1 + max_reinstatements)
    for target, occupancy in post_occupancy.items():
        if occupancy > ceiling + EPS:
            label = "事故#%s" % target if target > 0 else "待建事故"
            raise Conflict("%s累计摊回%s超过再保容量（含恢复）%s" % (label, round(occupancy, 2), round(ceiling, 2)))

    # 已结算赔案改归属：原结算依据不动，仅在双方事故台账记差额
    adjustments: List[Dict[str, Any]] = []
    for c in effective:
        if c["status"] != "settled":
            continue
        before = old_owner.get(c["id"])
        after = assignments[c["id"]]
        if before == after or not before:
            continue
        amount = round(float(c.get("settled_recovery") or 0.0), 2)
        before_count = reinstatements_used(pre_occupancy.get(before, 0.0), capacity, max_reinstatements)
        after_count = reinstatements_used(post_occupancy.get(after, 0.0), capacity, max_reinstatements)
        if before in active_ids and before not in closures and before != after:
            adjustments.append({
                "occurrence_key": before,
                "claim_id": c["id"],
                "delta_amount": -amount,
                "note": "已结算赔案保留原结算依据，归属转出至事故#%s，恢复次数%s->%s"
                        % (after if after > 0 else "新", before_count, after_count),
            })
        adjustments.append({
            "occurrence_key": after,
            "claim_id": c["id"],
            "delta_amount": amount,
            "note": "已结算赔案并入，保留原结算依据只记差额，恢复次数%s->%s" % (before_count, after_count),
        })

    return {
        "assignments": assignments,
        "new_occurrences": new_occurrences,
        "merges": merges,
        "closures": closures,
        "adjustments": adjustments,
        "window_edges": {str(key): [to_iso(edge[0]), to_iso(edge[1])] for key, edge in window_edges.items()},
        "post_occupancy": post_occupancy,
    }
