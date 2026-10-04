# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误（含冲突上下文）、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量，以及转移结算纯逻辑（单一可结算量、冻结指纹、结算阶段）。
- `src/repository.py`：SQLite建表、零基线迁移、事务、版本控制、审计链、余量台账、活动锁与分阶段幂等结算。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败、转移结算与迁移测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。旧库启动时自动迁移：新增的
`quota_balance`、`frozen_version`、`frozen_by_transfer`、整改`hold_amount`
字段按**零基线**回填（默认0/NULL），不改动历史业务数据。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`（可带`quota_balance`初始化余量台账）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`（整改记录可带`hold_amount`占用量）
- `POST /api/items/{id}/records/{rid}/close`：关闭整改
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/quantity`：修改许可数量，必须提交`expected_version`
- `GET  /api/items/{id}/quota`：余量台账、整改占用与合并后的可转移量
- `POST /api/transfers`：受理转移，冻结双方版本
- `POST /api/transfers/{id}/confirm`：确认结算（分阶段幂等，可凭结算单续办）
- `POST /api/transfers/{id}/release`：释放未确认冻结（已部分结算的自动冲正）
- `GET  /api/transfers?item_id=`、`GET /api/transfers/{id}`：结算单（含依据basis、影响impact）
- `GET  /api/quota-ledger?item_id=`：余量台账流水
- `GET /api/audit`

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 转移结算语义

- **不重复扣余量**：可转移量 = 余量台账余额 − 未关闭整改占用（`hold_amount`），
  台账与整改合并成一个口径，结算只扣一次，超额申请在受理时被拒。
- **受理即冻结**：冻结双方许可的数量、状态、余量、版本与整改记录指纹（basis），
  并对双方加活动锁（`transfer_locks`）。
- **变更即释放重算**：许可数量、状态或整改记录一旦变化，未确认（`frozen`）
  的结算单在同一事务内释放并解锁；若已发生部分扣减，自动写冲正台账回滚。
- **已结算保留依据与影响范围**：`settled`单不可释放/修改，basis与impact
  （双方台账流水ID、结算后余额/版本）永久保留。
- **并发先到先得**：两人同时提交同一许可时，活动锁唯一索引保证先提交者成功；
  晚到者收到409，响应`context`只包含当前余量与冲突结算单，不泄漏对方明细。
- **分阶段幂等结算**：`debited`（出方扣减）→`credited`（入方记入）→
  `settled`（落账归档），每阶段独立事务并有唯一索引兜底。任一阶段写入失败后
  凭结算单调confirm，从断点继续，已完成阶段跳过，重提不会多扣。
- 冻结依据（许可数量/状态/余量或整改记录）在首个阶段前校验，漂移即释放重算。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

