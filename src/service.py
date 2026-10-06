"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, number, text
from .ledger import (
    CLAIM_ACTIONS,
    role_can_create_contract,
    role_can_ledger_action,
    validate_claim_payload,
    validate_contract_payload,
)
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---------- 巨灾事故台账 ----------
    def _ledger_actor(self, actor: Actor) -> Actor:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return actor

    def create_contract(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._ledger_actor(actor)
        if not role_can_create_contract(actor.role):
            raise PermissionDenied("角色无权创建再保合约")
        reference = text({"reference": reference}, "reference")
        prepared = validate_contract_payload(payload or {})
        return self.repository.create_contract_row(reference, prepared, actor.user_id)

    def list_contracts(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._ledger_actor(actor)
        return self.repository.list_contract_rows(limit=limit)

    def get_contract(self, actor: Actor, contract_id: int) -> Dict[str, Any]:
        actor = self._ledger_actor(actor)
        return self.repository.get_contract_row(contract_id)

    def register_claim(self, actor: Actor, contract_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._ledger_actor(actor)
        if not role_can_ledger_action(actor.role, "register_claim"):
            raise PermissionDenied("角色无权登记赔案")
        prepared = validate_claim_payload(payload or {})
        return self.repository.register_claim_row(int(contract_id), prepared, actor.user_id)

    def ledger_action(self, actor: Actor, claim_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._ledger_actor(actor)
        action = text({"action": action}, "action")
        if action not in CLAIM_ACTIONS:
            raise PermissionDenied("未知赔案动作")
        if not role_can_ledger_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        if not isinstance(expected_version, int):
            from .domain import ValidationError
            raise ValidationError("expected_version必须是整数")
        action_data = dict(data or {})
        if action == "approve_claim":
            action_data["approved_loss"] = number(action_data, "approved_loss", 0)
        elif action == "settle_claim":
            action_data["payment_reference"] = text(action_data, "payment_reference")
        elif action == "reject_claim":
            action_data["reject_reason"] = text(action_data, "reject_reason")
        return self.repository.act_on_claim(int(claim_id), expected_version, action, action_data, actor.user_id)

    def list_claims(self, actor: Actor, contract_id: int) -> List[Dict[str, Any]]:
        actor = self._ledger_actor(actor)
        return self.repository.list_claim_rows(int(contract_id))

    def get_claim(self, actor: Actor, claim_id: int) -> Dict[str, Any]:
        actor = self._ledger_actor(actor)
        return self.repository.get_claim_row(int(claim_id))

    def list_occurrences(self, actor: Actor, contract_id: int) -> List[Dict[str, Any]]:
        actor = self._ledger_actor(actor)
        return self.repository.list_occurrence_views(int(contract_id))

    def list_adjustments(self, actor: Actor, contract_id: Optional[int] = None) -> List[Dict[str, Any]]:
        actor = self._ledger_actor(actor)
        return self.repository.list_adjustments(int(contract_id) if contract_id else None)

    def ledger_timeline(self, actor: Actor, contract_id: int) -> List[Dict[str, Any]]:
        actor = self._ledger_actor(actor)
        return self.repository.ledger_timeline(int(contract_id))

    def reconcile(self, actor: Actor) -> Dict[str, int]:
        """管理员手动重放 pending 合并任务，恢复一致台账。"""
        actor = self._actor(actor)
        if not role_can_ledger_action(actor.role, "reconcile"):
            raise PermissionDenied("角色无权重放台账任务")
        return {"replayed_jobs": self.repository.recover_pending_jobs()}
