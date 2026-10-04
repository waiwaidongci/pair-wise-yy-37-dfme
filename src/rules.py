from __future__ import annotations
import hashlib
import json
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
# 转移结算：许可申请角色发起，合规管理员确认/释放；查看角色可看结算单
TRANSFER_CREATE_ROLES=set(['applicant']); TRANSFER_CONFIRM_ROLES=set(['compliance_manager']); TRANSFER_RELEASE_ROLES=set(['applicant','inspector','compliance_manager'])
TRANSFER_STATES=['frozen','settled','released']
# 结算分阶段落账，每阶段独立事务、可凭结算单从断点续办：
# debited=出方已扣 / credited=入方已记 / settled=已落账并归档冻结
TRANSFER_STAGES=['debited','credited','settled']
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

# ---------- 转移结算纯逻辑 ----------
def settleable_amount(quota_balance, hold_amount):
    """余量台账与整改占用合并为单一可结算量：
    旧实现把两者分开各扣一遍同一余量，这里统一成一个口径，结算只扣一次。"""
    available=float(quota_balance)-float(hold_amount)
    return max(0.0, available)

def validate_transfer_amount(amount, quota_balance, hold_amount):
    if isinstance(amount,bool) or amount is None:
        raise ValidationError("amount必须是数字")
    try:
        amount=float(amount)
    except (TypeError,ValueError):
        raise ValidationError("amount必须是数字")
    if amount<=0:
        raise ValidationError("转移量必须大于0")
    available=settleable_amount(quota_balance, hold_amount)
    if amount>available+1e-9:
        raise ConflictError(
            "可转移余量不足（余量台账与整改占用已合并计算，同一余量不重复扣减）",
            context={"available": available, "quota_balance": float(quota_balance),
                     "hold_amount": float(hold_amount), "requested": amount})
    return amount

def records_fingerprint(records):
    """整改记录的占用口径指纹：未关闭整改及其占用量。
    冻结后只要未关闭整改集合或占用量变化，指纹即不一致，冻结作废重算。"""
    open_items=sorted(
        ({"id": int(r["id"]), "kind": str(r["kind"]),
          "hold_amount": round(float(r.get("hold_amount") or 0.0), 6)})
        for r in records if r.get("status")=="open")
    raw=json.dumps(open_items, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def total_hold_amount(records):
    return round(sum(float(r.get("hold_amount") or 0.0)
                     for r in records if r.get("status")=="open"), 6)

def item_fingerprint(item):
    """许可冻结口径：数量、状态、余量余额、版本构成快照。"""
    raw=json.dumps({
        "quantity": float(item["quantity"]), "status": item["status"],
        "quota_balance": float(item.get("quota_balance") or 0.0),
        "version": int(item["version"]),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def next_stage(stage):
    if stage is None:
        return TRANSFER_STAGES[0]
    if stage not in TRANSFER_STAGES:
        raise ValidationError("未知结算阶段")
    index=TRANSFER_STAGES.index(stage)
    return TRANSFER_STAGES[index+1] if index+1<len(TRANSFER_STAGES) else None

def stage_done(stage, target):
    """stage进度是否已达到target（幂等续办判定）。"""
    order={name: i for i, name in enumerate(TRANSFER_STAGES)}
    if stage is None:
        return False
    return order[stage]>=order[target]

