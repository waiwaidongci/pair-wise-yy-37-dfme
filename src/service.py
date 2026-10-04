from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .domain import (ConflictError, NotFoundError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    TRANSFER_ACCEPT_ROLES, TRANSFER_RECALC_ROLES,
                    TRANSFER_SETTLE_ROLES, TRANSFER_SUBMIT_ROLES,
                    TRANSFER_VIEW_ROLES, VIEW_ROLES, completion_blockers,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transfer_amount, validate_transition)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def calculate_hash(previous_hash: str, payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256((previous_hash + ":").encode("utf-8") + raw).hexdigest()


def make_entry(action: str, entity_type: str, entity_id: int, actor: str,
               detail: dict, previous_hash: str) -> dict:
    payload = {
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "actor": actor,
        "detail": detail,
        "created_at": utc_now(),
    }
    return dict(payload, previous_hash=previous_hash,
                entry_hash=calculate_hash(previous_hash, payload))


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ---- 项目 ----

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def update_quantity(self, item_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        quantity = require_number(payload.get("quantity"), "quantity")
        item = self.repository.update_item_quantity(item_id, quantity, actor)
        self.repository.append_audit("quantity_update", ENTITY, item_id, actor, {
            "quantity": quantity,
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
        deduction = require_number(payload.get("deduction", 0), "deduction")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor, deduction)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
            "deduction": deduction,
        })
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
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
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

    def get_allowance(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_allowance(item_id)

    # ---- 转移结算 ----

    def submit_transfer(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        source_item_id = payload.get("source_item_id")
        target_item_id = payload.get("target_item_id")
        if not isinstance(source_item_id, int) or source_item_id < 1:
            raise ValidationError("source_item_id必须是正整数")
        if not isinstance(target_item_id, int) or target_item_id < 1:
            raise ValidationError("target_item_id必须是正整数")
        if source_item_id == target_item_id:
            raise ValidationError("转出方和转入方不能是同一许可")
        amount = require_number(payload.get("amount"), "amount", 0.000001)
        idempotency_key = payload.get("idempotency_key")
        if idempotency_key is not None:
            idempotency_key = require_text(idempotency_key, "idempotency_key", 100)
        else:
            idempotency_key = uuid.uuid4().hex
        # 幂等：同 key 重提返回已有结算单，不会多扣
        existing = self.repository.get_transfer_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing
        self.repository.get_item(source_item_id)
        self.repository.get_item(target_item_id)
        allowance_info = self.repository.get_allowance(source_item_id)
        amount = validate_transfer_amount(amount, allowance_info["allowance"])
        transfer = self.repository.create_transfer(
            source_item_id, target_item_id, amount,
            allowance_info["allowance"], idempotency_key, actor)
        self.repository.append_audit("transfer_submit", "transfer", transfer["id"], actor, {
            "source_item_id": source_item_id, "target_item_id": target_item_id,
            "amount": amount, "allowance": allowance_info["allowance"],
            "idempotency_key": idempotency_key,
        })
        return transfer

    def accept_transfer(self, transfer_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_ACCEPT_ROLES)
        actor = require_text(actor, "actor", 100)
        transfer = self.repository.accept_transfer_atomic(transfer_id)
        self.repository.append_audit("transfer_accept", "transfer", transfer["id"], actor, {
            "source_item_id": transfer["source_item_id"],
            "target_item_id": transfer["target_item_id"],
            "source_version": transfer["source_version"],
            "target_version": transfer["target_version"],
            "allowance": transfer["allowance"],
        })
        return transfer

    def settle_transfer(self, transfer_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_SETTLE_ROLES)
        actor = require_text(actor, "actor", 100)
        transfer = self.repository.settle_transfer_atomic(transfer_id)
        self.repository.append_audit("transfer_settle", "transfer", transfer["id"], actor, {
            "source_item_id": transfer["source_item_id"],
            "target_item_id": transfer["target_item_id"],
            "amount": transfer["amount"],
            "basis": transfer.get("basis", {}),
            "impact_scope": transfer.get("impact_scope", {}),
        })
        return transfer

    def recalculate_transfer(self, transfer_id: int, actor: str,
                             role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_RECALC_ROLES)
        actor = require_text(actor, "actor", 100)
        transfer = self.repository.recalculate_transfer(transfer_id)
        self.repository.append_audit("transfer_recalculate", "transfer", transfer["id"], actor, {
            "source_item_id": transfer["source_item_id"],
            "allowance": transfer["allowance"],
        })
        return transfer

    def get_transfer(self, transfer_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_VIEW_ROLES)
        return self.repository.get_transfer(transfer_id)

    def list_transfers(self, role: str, status: Optional[str] = None,
                       source_item_id: Optional[int] = None,
                       target_item_id: Optional[int] = None) -> list:
        ensure_role(role, TRANSFER_VIEW_ROLES)
        return self.repository.list_transfers(status, source_item_id, target_item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

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
