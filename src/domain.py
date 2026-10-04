from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message,details=None):
        super().__init__(message); self.message=message; self.details=details or {}
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; ROLES=['applicant', 'inspector', 'compliance_manager', 'viewer']
TRANSFER_STATES=['submitted', 'frozen', 'settled', 'released', 'failed']
FREEZE_STATES=['active', 'released', 'settled']
LEDGER_DIRECTIONS=['out', 'in']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; deduction:float; created_by:str; created_at:str
@dataclass(frozen=True)
class Transfer:
    id:int; source_item_id:int; target_item_id:int; amount:float; status:str
    source_version:int; target_version:int; source_quantity:float; target_quantity:float
    allowance:float; basis:Dict[str,Any]; impact_scope:Dict[str,Any]
    idempotency_key:Optional[str]; progress:Dict[str,Any]
    created_by:str; created_at:str; updated_at:str; settled_at:Optional[str]
@dataclass(frozen=True)
class Freeze:
    id:int; item_id:int; transfer_id:int; version:int; status:str; created_at:str; released_at:Optional[str]
@dataclass(frozen=True)
class LedgerEntry:
    id:int; item_id:int; transfer_id:int; amount:float; direction:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
