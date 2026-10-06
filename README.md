# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/occurrences.py`：巨灾小时条款的时间解析、窗口归组与恢复次数纯函数。
- `src/rules.py`：状态转换、分层摊回、赔偿限额和恢复保费和冲突检查。
- `src/repository.py`：SQLite建表、事务、事故台账与启动对账。
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
- `GET /api/occurrences`：巨灾事故台账，可带`event_id`过滤。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

## 巨灾事故小时条款

- 创建合约时可在`data.event_hours`中约定同一事故的小时窗口，默认72小时。
- `submit_claim`必须携带`occurred_at`（ISO8601），赔案按发生时刻并入事故台账：
  与同灾害任一事故窗口两端的间隔不超过约定小时即并入；一个晚到赔案同时
  贴近两起事故时，把它们桥接合并为同一次事故。
- 事故归属变化后，未结算赔案按新事故重算累计占用`occurrence_recovery`和
  恢复次数`reinstatements_used`并升版本、记`occurrence_recomputed`审计；
  已结算赔案保留结算时的冻结依据（`settled_occurrence_recovery`、
  `settled_reinstatements_used`），只追加`occurrence_delta`差额审计。
- 报案事务使用`BEGIN IMMEDIATE`串行化，同一窗口并发报案只建一次事故；
  合并中途失败整体回滚，服务重启时自动对账修复半成品合并（赔案改挂根事故、
  贴近事故补合并、占用与恢复重算）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
