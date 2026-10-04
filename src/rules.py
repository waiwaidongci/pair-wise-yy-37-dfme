from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
# 转移结算状态机：提交 -> 冻结 -> 结算；任一变更导致冻结释放 -> 重算；失败可恢复
TRANSFER_STATES=['submitted', 'frozen', 'settled', 'released', 'failed']
TRANSFER_TRANSITIONS={
    'submitted': ['frozen', 'failed'],
    'frozen': ['settled', 'released', 'failed'],
    'released': ['submitted', 'failed'],
    'failed': ['submitted'],
    'settled': [],
}
# 结算单进度步骤（用于失败恢复，接着上次进度办）
SETTLE_STEPS=['verify', 'deduct', 'finalize']
# 转移结算角色：申请人提交，合规管理员受理/结算
TRANSFER_SUBMIT_ROLES=set(['applicant', 'compliance_manager'])
TRANSFER_ACCEPT_ROLES=set(['compliance_manager'])
TRANSFER_SETTLE_ROLES=set(['compliance_manager'])
TRANSFER_RECALC_ROLES=set(['applicant', 'compliance_manager'])
TRANSFER_VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
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
def can_transfer_transition(current,target): return target in TRANSFER_TRANSITIONS.get(current,[])
def validate_transfer_transition(current,target):
    if current not in TRANSFER_STATES or target not in TRANSFER_STATES: raise ValidationError("未知转移状态")
    if not can_transfer_transition(current,target): raise ConflictError(f"转移单不能从{current}转换到{target}")
def remaining_allowance(quantity, settled_out=0.0, rectification_deductions=0.0):
    """计算许可剩余排放指标。

    余量 = 许可数量 - 台账已结算转出 - 整改抵扣。
    台账与整改分开算会重复扣同一余量，因此受理时冻结版本、按冻结口径一次性算定。
    """
    return max(0.0, float(quantity) - float(settled_out) - float(rectification_deductions))
def validate_transfer_amount(amount, allowance):
    if not isinstance(amount,(int,float)) or isinstance(amount,bool): raise ValidationError("转移数量必须是数字")
    amount=float(amount)
    if amount<=0: raise ValidationError("转移数量必须大于0")
    if amount>allowance: raise ConflictError("转移数量超过剩余余量", {"allowance": allowance})
    return amount
