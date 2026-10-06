"""巨灾事故小时条款：时间解析、窗口归组与恢复次数的纯领域函数。

事故（occurrence）认定规则：同一灾害（event_id）下，赔案发生时刻与已有
事故窗口两端的间隔不超过合约约定小时数，即并入该事故；一个晚到赔案同时
贴近两起事故时，将它们合并为同一次事故。
"""
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import ValidationError

EPSILON_HOURS = 1e-9


def parse_time(value: str) -> datetime:
    """解析ISO8601时间，无时区按UTC处理，统一返回UTC datetime。"""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("occurred_at不能为空")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError("occurred_at必须是ISO8601时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def gap_hours(point: datetime, started_at: datetime, ended_at: datetime) -> float:
    """赔案发生时刻到事故窗口[started_at, ended_at]最近一端的小时间隔。"""
    if point < started_at:
        return (started_at - point).total_seconds() / 3600.0
    if point > ended_at:
        return (point - ended_at).total_seconds() / 3600.0
    return 0.0


def plan_assignment(events: List[Dict[str, Any]], occurred_at: datetime, event_hours: float) -> Dict[str, Any]:
    """给定同灾害下全部有效事故，决定新赔案的归属动作。

    返回：
      {"kind": "new"}                              新建事故
      {"kind": "join", "occurrence_id": id}        并入唯一贴近事故
      {"kind": "merge", "survivor_id": id,
       "occurrence_ids": [ids]}                    桥接多起事故，全部合并
    """
    touching: List[int] = []
    for event in events:
        gap = gap_hours(occurred_at, event["started_dt"], event["ended_dt"])
        if gap <= float(event_hours) + EPSILON_HOURS:
            touching.append(int(event["id"]))
    if not touching:
        return {"kind": "new"}
    if len(touching) == 1:
        return {"kind": "join", "occurrence_id": touching[0]}
    survivor = min(touching)
    return {"kind": "merge", "survivor_id": survivor, "occurrence_ids": sorted(touching)}


def reinstatements_used(cumulative_before: float, amount: float, layer_width: float) -> int:
    """该笔赔案计入时新增消耗的分层恢复次数。

    按事故内赔案发生顺序累计摊回。原始保障层（累计不超过一个分层宽度）
    内恢复次数为0；之后每多占满一个分层宽度，需要一次恢复，一笔大额
    赔案可一次跨越多档。
    """
    if layer_width <= 0:
        return 0

    def layers(value: float) -> int:
        # 恰好等于整数倍层宽时记为该层刚用尽，不提前消耗下一恢复档
        return max(0, math.ceil(value / layer_width - 1e-9) - 1)

    return max(0, layers(cumulative_before + amount) - layers(cumulative_before))
