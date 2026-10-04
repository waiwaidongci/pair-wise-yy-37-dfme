from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    TRANSFER_CONFIRM_ROLES, TRANSFER_CREATE_ROLES,
                    TRANSFER_RELEASE_ROLES, TRANSFER_STAGES, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        quota_balance = require_number(payload.get("quota_balance", 0), "quota_balance")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor,
                                           quota_balance)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "quota_balance": quota_balance,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        hold_amount = require_number(payload.get("hold_amount", 0), "hold_amount")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor, hold_amount)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
            "hold_amount": hold_amount,
        })
        return record

    def close_record(self, item_id: int, record_id: int, actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        record = self.repository.close_record(item_id, record_id, actor)
        self.repository.append_audit("record_close", ENTITY, item_id, actor, {
            "record_id": record_id})
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        # 状态变更时仓储在同一事务内释放相关未确认冻结（含冲正与审计）
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def update_quantity(self, item_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        """许可数量变更：与状态、整改变更同级，仓储在同一事务内释放未确认冻结。"""
        ensure_role(role, {"applicant", "inspector", "compliance_manager"})
        actor = require_text(actor, "actor", 100)
        quantity = require_number(payload.get("quantity"), "quantity")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        updated = self.repository.update_quantity(
            item_id, quantity, expected_version, actor)
        self.repository.append_audit("quantity_change", ENTITY, item_id, actor, {
            "quantity": quantity, "expected_version": expected_version})
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---------- 转移结算 ----------
    def quota(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        summary = self.repository.quota_summary(item_id)
        lock = self.repository.active_lock_for(item_id)
        summary["active_lock"] = lock
        return summary

    def accept_transfer(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        source_item_id = require_number(payload.get("source_item_id"),
                                        "source_item_id", 1)
        target_item_id = require_number(payload.get("target_item_id"),
                                        "target_item_id", 1)
        source_item_id, target_item_id = int(source_item_id), int(target_item_id)
        if source_item_id == target_item_id:
            raise ValidationError("出方和入方不能是同一许可")
        amount = require_number(payload.get("amount"), "amount", 0.000001)
        key = payload.get("idempotency_key") or f"T-{source_item_id}-{target_item_id}-{amount}-{actor}"
        key = require_text(key, "idempotency_key", 120)
        transfer = self.repository.accept_transfer(
            source_item_id, target_item_id, amount, key, actor)
        return transfer

    def confirm_transfer(self, transfer_id: int, actor: str, role: str,
                         before_stage: Optional[Any] = None) -> Dict[str, Any]:
        """确认结算：顺序推进 出方扣减→入方记入→落账。
        每阶段独立事务；任一阶段写入失败，凭结算单重入从断点继续，
        已完成阶段幂等跳过，重提不会多扣。
        before_stage: 可选回调 fn(stage:str, transfer_id:int)，
        在每个阶段执行前调用；抛异常即模拟该阶段写入失败。"""
        ensure_role(role, TRANSFER_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        transfer = self.repository.get_transfer(transfer_id)
        if transfer["state"] == "settled":
            return transfer
        for stage in TRANSFER_STAGES:
            if before_stage is not None:
                before_stage(stage, transfer_id)
            transfer = self.repository.settle_step(transfer_id, stage, actor)
            if transfer["state"] != "frozen":
                break
        return transfer

    def release_transfer(self, transfer_id: int, actor: str,
                         role: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_RELEASE_ROLES)
        actor = require_text(actor, "actor", 100)
        reason = (payload or {}).get("reason") or "人工释放未确认冻结"
        reason = require_text(reason, "reason", 300)
        return self.repository.release_transfer(transfer_id, actor, reason)

    def get_transfer(self, transfer_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_transfer(transfer_id)

    def list_transfers(self, role: str, item_id: Optional[int] = None) -> list:
        self._view(role)
        return self.repository.list_transfers(item_id)

    def list_ledger(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_ledger(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
