# budget

预算与费用审批 app：把 `dingtalk-budget`（Express）与 `dingtalk-expense-sync`（TypeScript）
两个 Node 项目重写成一套 Python 实现，按 DeepLinkERP 的分层纪律组织：

> **命名**：app 按业务域命名（对齐 `china_finance` / `overseas_costing` / `mes_integration`），
> 不按数据源命名。钉钉与 `dingtalk_oa` 库只是数据来源，不是这个 app 的边界。

模块文档：[docs/README.md](docs/README.md) — 分层、接口清单、配置、错误处理、时序。


```
api/（只做参数入口 + 权限 + 结果封装）
  ↓
services/（判定逻辑真源：归因、分页游标、唯一性、分层）
  ↓
integrations/（IO：钉钉 HTTP、OA 只读库）
```

本 app 不改上游 app、不 `ALTER TABLE`，读写都通过声明与 patch 完成。

## 与既有实现的关系

bench 里此前有三套重复的钉钉/OA 代码，本 app 作为唯一入口，逐步替代：

| 既有实现 | 现状 | 本 app 的对应件 |
| --- | --- | --- |
| `draft_notifications/dingtalk_robot.py` | token 用 `frappe.cache()` 缓存 + 机器人发消息 | `integrations/dingtalk_client.py`（token 缓存自动适配 `frappe.cache()`） |
| `overseas_costing/integrations/dingtalk_approval_source.py` | psycopg3 只读直连 OA 库 | `integrations/oa_approval_source.py`（同样的 `read_only` 会话与注入式 `connect`） |
| `oa_purchase_request/...py:729` | 旧版 gettoken + `subprocess psql` 查 OA 库 | 由上面两件替代（**不要再新增 psql 子进程写法**） |

迁移策略：新代码只走本 app；存量调用点按业务优先级逐个切换，切换完再删旧实现。
在切换完成前，两边并存但不互相 import。

## 上游契约与消费方式

数据由 `yueweiit/dingtalk-oa` 拉取并写入 `dingtalk_oa` 库，**它是唯一写方**。
本 app 只消费，不改它的库结构、不写它的表、不重写拉取逻辑。
上游缺能力时**给它提 PR 新增接口**，不在 Python 侧绕过或复制实现。

| 需要的数据 | 走哪 | 原因 |
| --- | --- | --- |
| 审批列表 / 详情 / 附件 | **HTTP** `/api/v1/approvals`、`/attachments`、`/attachment-content` | 上游正式契约，cursor 分页 + `syncUntil` 水位 |
| 部门树、用户快照 | 只读库 | API 未提供 |
| 任务明细、审批效率 | 只读库 `ding_approval_task` | API 只把 tasks 放在 `rawPayload` 里，没有聚合接口 |

增量水位按上游契约算（`approval-read-api/README.md:150-157`）：

```python
from budget.services.approval_watermark_service import compute_next_watermark

next_updated_after = compute_next_watermark(page.sync_until)  # 自动回退 10 分钟重叠
```

**不要**用 `last_event_time` 当水位——数据字典明确它是「规范化时的本地刷新时间，并非严格的上游事件时间」。
`updated_at` 由触发器维护（源内容不变时保留旧值），才是可用的增量依据。

## 需要上游新增的接口（待提 PR）

| 缺口 | 影响 |
| --- | --- |
| 审批列表无 `status` / `createTime` 区间过滤 | 只能按 `updatedAfter` 拉全量再本地筛 |
| 无部门树接口 | 必须读库 |
| 无审批效率 / 聚合接口 | 必须读库并自己算 |
| 审批响应无顶层 `tasks` | 需解析 `rawPayload.tasks` 或读库 |

## 目录

```
budget/
├─ hooks.py                  # 目前只声明 app 元信息，不挂 doc_events
├─ api/approval.py           # 只读接口：get_ / list_ 前缀（前端可安全重试）
├─ services/                 # 纯逻辑，可脱 Frappe 单测
│  ├─ approval_attribution_service.py
│  ├─ approval_watermark_service.py   # 增量水位（syncUntil 回退重叠窗口）
│  ├─ organization_service.py
│  └─ approval_query_service.py
├─ integrations/
│  ├─ dingtalk_client.py     # token 缓存 + 机器人发送 + 重试 + 三组熔断
│  ├─ oa_read_api_client.py  # 上游只读 HTTP API（审批快照 / cursor 分页）
│  └─ oa_approval_source.py  # OA 只读库（psycopg3），仅补 API 缺口
└─ tests/                    # pytest：全部靠注入替身，不需要站点/库/网络
```

## 覆盖的接口与查询

| 清单 | 实现 |
| --- | --- |
| 实例列表 / 增量 | `OAReadAPIClient.iter_approval_pages`（HTTP，上游 cursor） |
| ① token | `DingTalkClient.get_access_token`（企业应用 / 机器人两套凭据） |
| ⑥ 机器人 Markdown | `send_markdown` |
| Q4 / Q5 详情 | `get_instance_row(with_snapshot=False/True)` |
| Q6 部门树 | `list_departments` + `organization_service.build_organization_options` |
| Q7 部门反查 | `list_department_candidates` + `pick_unique_department` |
| Q8 效率报表 | `list_approval_efficiency_rows` |
| Q9 路径快照 | `list_department_snapshot_rows` + `normalize_department_snapshots` |

## 安装与入口

```bash
bench --site <site> install-app budget      # 已装则跳过
bench --site <site> migrate                 # 同步 DocType / Page，并补侧边栏入口
bench --site <site> build --app budget      # 生成 /assets/budget（Page 脚本从这里加载）
bench --site <site> clear-cache
```

入口挂在 ERPNext「会计」侧边栏的 **Budget** 分组下：预算台账（DocType）、预算数据看板
（`/app/budget-dashboard`）、预算配置（`/app/budget-config`）。`after_install` 与
`after_migrate` 都会幂等重放，`install-app` 之后仍需跑一次 `migrate`（Page 记录与脚本
是 migrate 时才同步的，缺了会表现为「点进去空白」）。

找不到入口时先单独补一次：

```bash
bench --site <site> execute budget.setup.ensure_budget_sidebar_link
```

看板要求 `System Manager` 或 `Accounts Manager`，预算配置仅 `System Manager`。

## 配置

站点只配**一套** PostgreSQL 连接，三个只读数据源（OA / 预算台账 / 审批支出）共用
`shared_db_config()`（环境变量优先，其次 `frappe.conf`）：

```
BUDGET_DB_HOST / BUDGET_DB_PORT / BUDGET_DB_NAME
BUDGET_DB_USER / BUDGET_DB_PASSWORD
BUDGET_CORP_ID           # 本实例负责的企业，必填
```

`BUDGET_CORP_ID` 是查询范围，不是可选优化：OA 库多企业共库，`ding_department_tree`
唯一键是 `(corp_id, dept_id)`，`businessId` 也可能跨企业重复。未配置时所有查询直接抛
`OAApprovalSourceScopeError`，不会退化成读全库。

兼容既有键名 `budget_oa_db_*` / `dingtalk_oa_db_*` / `oa_mapping_pg_*`；
企业范围兼容 `budget_oa_corp_id` / `dingtalk_oa_corp_id` / `dingtalk_corp_id`。
老站点不用改配置即可运行。

库名按「数据源自己的键 → `BUDGET_DB_NAME` → 该数据源默认库名」解析：

| 数据源 | 库名键（可选） | 未配置时的默认 |
| --- | --- | --- |
| OA 审批 / 部门树 | —（直接用 `BUDGET_DB_NAME`） | `dingtalk_oa` |
| 预算台账 | `BUDGET_LEDGER_DB_NAME`（旧：`budget_system_db_name`） | `budget_system` |
| 审批支出 | `BUDGET_APPROVAL_DB_NAME`（旧：`dingtalk_approval_db_name`） | `dingtalk_approval` |

三个库合并成一个之后，把 `BUDGET_DB_NAME` 指向合并后的库、删掉表格里两个「数据源自己的键」即可，
连接信息始终只有一处。

> 注意：台账数据同步后落在**站点自己的 MariaDB**（`Budget Ledger Entry`），
> 列表、筛选、排序、导出都不依赖上面这些外部连接；外部 PG 只在同步和审批查询时用。

上游只读 API（走 HTTP 时必填）：

```
BUDGET_OA_SOURCE         # postgres（默认）| http
BUDGET_OA_API_URL        # 如 https://oa.example.com
BUDGET_OA_API_TIMEOUT    # 秒，默认 30
BUDGET_OA_API_PAGE_SIZE  # 每页条数，默认 200（接口上限 200）
```

`BUDGET_OA_SOURCE=http` 时审批读取（Q4/Q5/Q8）改走 `dingtalk-oa` 的
`GET /api/v1/approvals`，`DingTalk` → Kafka → PG 的归档链路不变；

- 两个数据源暴露同一组方法（`get_instance_row` / `list_approval_efficiency_rows` 等），
  业务层通过 `oa_factory.get_oa_source()` 取实例，不感知来源；
- 该接口只覆盖审批，没有部门树：Q6/Q7/Q9 与 Q5 的用户快照名仍回退到 PostgreSQL，
  库不可达时快照名降级为空（用接口原始名称），不影响查询；
- 接口不支持按 `createTime` 过滤，时间窗本地过滤，并用 `updatedAfter` 先收窄一页范围。

`BUDGET_OA_API_URL` 兼容既有的 `operating_expense_oa_api_url` 与 `budget_oa_api_base`，
避免同一站点配两处地址。

钉钉凭据（`frappe.conf` 或环境变量，`DingTalkConfig.from_site_config()`）：

```
dingtalk_app_key / dingtalk_app_secret
dingtalk_robot_app_key / dingtalk_robot_app_secret / dingtalk_robot_code
```

OA 连接一律 `default_transaction_read_only=on` + `statement_timeout`，账号请只授只读权限。

## 安装与测试

```bash
# 安装（在容器内）
bench --site dev.localhost install-app budget
bench --site dev.localhost clear-cache

# 测试（不需要站点）
python3 -m pytest -q apps/budget/budget/tests
```

## 阶段规划

- **第一阶段（已完成）**：只读链路 + 纯逻辑归因 + 只读接口 + 单测。
- **第二阶段**：同步账本 DocType（`PENDING / RUNNING / SUCCESS / FAILED / UNCERTAIN / MANUAL_REQUIRED`
  状态机 + `payload_hash` 幂等），`*_ledger_service.py` 负责落库，`hooks.py` 登记 cron，
  `UNCERTAIN` 靠回读 OA 库 reconcile。
- **第三阶段**：业务层（预算额度、表单解析、支出拆分）从两个 Node 项目迁入，
  按 `*_service.py`（纯逻辑）+ `*_ledger_service.py`（IO）成对组织。
