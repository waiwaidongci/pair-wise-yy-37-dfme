# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量和转移结算规则。
- `src/repository.py`：SQLite建表、事务、版本控制、冻结释放、结算进度和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败和转移结算测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/quantity`，变更许可数量（触发冻结释放）
- `GET /api/items/{id}/allowance`，查看剩余排放指标
- `GET /api/transfers`，转移结算单列表
- `POST /api/transfers`，提交转移单（支持`idempotency_key`幂等）
- `GET /api/transfers/{id}`，查看结算单
- `POST /api/transfers/{id}/accept`，受理冻结双方版本
- `POST /api/transfers/{id}/settle`，结算（凭结算单恢复，重提不多扣）
- `POST /api/transfers/{id}/recalculate`，释放后重算
- `GET /api/audit`

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 转移结算

快到期许可的剩余排放指标可转给在建许可。余量台账与整改记录分开算会重复扣同一余量，因此受理时冻结双方版本，按冻结口径一次性算定。

- **冻结释放**：许可数量、状态或整改一变，未确认冻结即释放重算；已结算的保留依据（basis）和影响范围（impact_scope）。
- **并发锁**：两人同时提交同一许可，先提交者锁住，晚到者只看到余量和冲突（409）。
- **失败恢复**：结算单按`verify`→`deduct`→`finalize`分步骤持久化进度，写入失败后凭结算单从上次进度继续，重提不会多扣。
- **零基线回填**：旧数据缺冻结字段，升级时`ALTER TABLE ADD COLUMN`按`DEFAULT 0`回填，已有许可自动获得零基线。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
