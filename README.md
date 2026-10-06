# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、分层摊回、赔偿限额和恢复保费和冲突检查。
- `src/ledger.py`：巨灾事故台账——小时条款链式聚类、容量占用与恢复次数计算。
- `src/ledger_repository.py`：台账建表、两阶段事故合并、pending任务与崩溃恢复。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 巨灾事故台账（小时条款）

合约按`hours_clause`约定小时数认定巨灾事故：同一合约下相邻赔案发生时刻间隔
不超过该小时数即链式归入同一次事故；晚到赔案若补上两次事故间的缺口，则两
次事故合并为一次。

- `POST /api/contracts`：建立再保合约（underwriter），`data`含`peril`、`hours_clause`、`attachment`、`limit_amount`、`cession_pct`、`reinstatement_pct`、`max_reinstatements`。
- `GET /api/contracts/{id}` / `GET /api/contracts`：合约详情/列表。
- `POST /api/contracts/{id}/claims`：按赔案发生时刻登记赔案（claims_officer），`data`含`claim_number`、`occurred_at`（ISO8601）、`loss_amount`；系统自动并入或新建事故。
- `POST /api/claims/{id}/actions/{approve_claim|settle_claim|reject_claim}`：核定/结算/拒绝，需`expected_version`。
- `GET /api/contracts/{id}/claims`：赔案列表。
- `GET /api/contracts/{id}/occurrences`：事故台账，含窗口起止、成员赔案、`occupancy`、`reinstatements_used`、剩余容量。
- `GET /api/contracts/{id}/timeline`：台账事件时间线（含`occurrences_merged`）。
- `GET /api/adjustments?contract_id={id}`：已结算赔案改归属时保留原结算依据、只记的差额分录。
- `POST /api/ledger/reconcile`：管理员重放pending合并任务。

归属改动时，未结算赔案按新事故重算容量占用与恢复次数；已结算赔案保留原结
算依据，只在新旧事故台账上记双向差额。事故合并分「意图提交」和「任务应用」
两个事务；应用阶段失败时意图与pending任务已落盘，进程重开自动重放（
`recover_pending_jobs`，幂等），同一窗口并发登记由`BEGIN IMMEDIATE`串行化，
且生效事故在`(合约, 窗口起点)`上有部分唯一索引，保证只建一次事故。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
