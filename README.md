# budget

预算与费用审批 app：把 `dingtalk-budget`（Express）与 `dingtalk-expense-sync`（TypeScript）
两个 Node 项目重写成一套 Python 实现，按 DeepLinkERP 的分层纪律组织：

> **命名**：app 按业务域命名（对齐 `china_finance` / `overseas_costing` / `mes_integration`），
> 不按数据源命名。钉钉与 `dingtalk_oa` 库只是数据来源，不是这个 app 的边界。

导航入口为“运营支出（CN）”（`China Operating Expenses`），包含预算台账、预算数据看板和预算配置；安装 `deeplinkerp_branding` 且已有运营支出工作台时，同时提供该页面入口。Sidebar、Desktop Icon 及每个 Page/DocType 仍按原生权限过滤，不额外授权业务数据。

安装新版 `deeplinkerp_branding` 后，CN 提供支出概览、支出明细、统计报表（支出分析、薪酬人事分析）和数据核对，原有三个预算页面收在“预算管理”分组。运营支出已复用中国财务原入口的申请、付款、核对和办理接口；预算、采购及薪酬统计仍显示待接入，不读取 MX 数据或模拟金额。详细统计口径和权限边界由品牌 App 的 `docs/operating-expenses-implementation.md` 维护。

页面由品牌 App 的 `public/js/china_expenses.js`、`public/js/china_operating_expenses.js`、`public/js/china_expenses_page.js` 和五个 `china_*` Page 承载；复用 MX 展示组件但保持独立路由、偏好及数据服务。`budget.setup.cn_navigation_items` 仅在五个 Page 均存在时启用新版侧栏；创建后调用品牌 App 的导航合并，移除中国财务系统管理的重复入口。旧运营 URL 和独立安装的回退入口保留。卸载预算 App 删除 CN 侧栏后，若品牌 App 仍存在，会立即恢复其原生备用入口，不依赖后续迁移。无需新增业务表。

ERPNext 的“预算”恢复在“会计”文件夹下，保留原生 Budget、Cost Center、Accounting Dimension、Cost Center Allocation、Budget Variance 五个入口。本 App 的 `setup.apply_budget_nav` 在安装/迁移时清理旧导航挂载，同步独立 CN 入口；`public/js/budget_navigation.js` 只清理这三个定制页面旧的 Budget 侧栏记忆。旧 URL、DocType、台账数据及接口均保留，不把 MX 排除明细自动写入国内财务数据。导航定义见 `budget/workspace_sidebar/china_operating_expenses.json` 和 `budget/desktop_icon/china_operating_expenses.json`。

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
| 部门树 / 部门反查 / 路径快照 | **HTTP** `/api/v1/budget/departments*` | 预算列表展示与审批归属都用它 |
| 预算台账 / 统计 / 明细 / 备注 | **HTTP** `/api/v1/budget/entries*` | 去重与状态口径在 dingtalk-oa 侧复刻 |
| 预算额度配置 | **HTTP** `/api/v1/budget/quota*` | 下拉候选 / 汇总 / 明细分页 |
| 实际支出（报表 + 详情） | **HTTP** `/api/v1/budget/expenses*` | 六大分支口径在 dingtalk-oa 侧复刻 |
| 任务明细、审批效率 | 从 `/api/v1/approvals` 的 `rawPayload.tasks` 解析 | 无独立聚合接口，本地聚合 |

**显式 HTTP 模式下 budget app 不建立任何 PostgreSQL 连接**：所有 SQL 集中在
`dingtalk-oa`，本 app 只做 HTTP 调用。`budget_data_source=postgres` 时才回退到
本仓库原有的 psycopg 直连实现（应急排查用）。

增量水位按上游契约算（`approval-read-api/README.md:150-157`）：

```python
from budget.services.approval_watermark_service import compute_next_watermark

next_updated_after = compute_next_watermark(page.sync_until)  # 自动回退 10 分钟重叠
```

**不要**用 `last_event_time` 当水位——数据字典明确它是「规范化时的本地刷新时间，并非严格的上游事件时间」。
`updated_at` 由触发器维护（源内容不变时保留旧值），才是可用的增量依据。

### 已在 dingtalk-oa 落地的预算接口

| 路由 | 用途 |
| --- | --- |
| `GET /api/v1/budget/entries` | 台账分页（`budgetType` / `startMonth` / `endMonth` / `status` / `keyword` / `page` / `pageSize`），返回 `rows / total / counts / total_amount` |
| `GET /api/v1/budget/entries/stats` | 今日生产 / 今日非生产 / 生产总数 / 非生产总数 |
| `GET /api/v1/budget/entries/{expense-details,production-details,remarks,detail-count}` | 明细、备注、明细行数 |
| `GET /api/v1/budget/quota/{options,summary,entries}` | 额度下拉候选、汇总、明细分页 |
| `GET /api/v1/budget/expenses/{approved,meta,completed,pending,splits,payments}` | 实际支出与报表聚合原料 |
| `GET /api/v1/budget/departments{,/candidates,/snapshots}` | 部门树、业务主体反查、路径快照 |

响应统一 `{success: true, data}`，失败 `{success: false, code}`，限流返回 429 + `Retry-After`。
字段名沿用原 PG 结果集的 snake_case，迁移时业务层不需要改名。

## 仍待上游补充的接口

| 缺口 | 影响 |
| --- | --- |
| 审批列表无 `status` / `createTime` 区间过滤 | 只能按 `updatedAfter` 拉全量再本地筛 |
| 无审批效率 / 聚合接口 | 本地按 `rawPayload.tasks` 聚合 |
| 审批响应无顶层 `tasks` | 需解析 `rawPayload.tasks` |
| 无用户快照名接口 | `snapshot_user_name` 恒为空，用接口原始名称展示 |

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
│  ├─ dingtalk_client.py          # token 缓存 + 机器人发送 + 重试 + 三组熔断
│  ├─ oa_read_api_client.py       # 审批只读 HTTP（快照 / cursor 分页）
│  ├─ budget_read_api_client.py   # 预算只读 HTTP（台账 / 额度 / 支出 / 部门树）
│  ├─ budget_factory.py           # 数据源选择：local（默认）| http | postgres
│  ├─ oa_factory.py               # 审批数据源选择（与上面共用开关）
│  ├─ *_api_source.py             # 各数据源的 HTTP 实现（统一签名）
│  ├─ *_source.py                 # 各数据源的 PG 直连实现（应急回退）
│  └─ pg.py                       # 仅 postgres 模式使用的只读连接池
└─ tests/                         # pytest：全部靠注入替身，不需要站点/库/网络
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

入口位于“运营支出（CN）”：预算台账（DocType）、预算数据看板
（`/desk/budget-dashboard`）、预算配置（`/desk/budget-config`）。`after_install` 与
`after_migrate` 都会幂等重放，`install-app` 之后仍需跑一次 `migrate`（Page 记录与脚本
是 migrate 时才同步的，缺了会表现为「点进去空白」）。

找不到入口时先单独补一次：

```bash
bench --site <site> execute budget.setup.ensure_budget_sidebar_link
```

看板要求 `System Manager` 或 `Accounts Manager`，预算配置仅 `System Manager`。

## 配置

### 预算台账同步（2026-11-19）

台账当前默认使用 `local`（见 `budget/integrations/budget_factory.py`），由
`services/oa_budget_sync.py` 只读读取 OA PostgreSQL 归档并解析到本地台账。
以下 HTTP 连接说明针对显式选择 HTTP 的接口适配器，不代表本地台账同步会使用 HTTP。

- 台账页面菜单提供“预算同步设置”，工具栏提供独立“同步数据”。保存只保存配置，
  打开台账只读，不自动排入同步；兼容接口 `ensure_ledger_fresh` 也不再入队。
- 在现有 `Budget Settings` 中配置 `ledger_corp_id`、`ledger_connection_profile`
  和 `ledger_sync_enabled`。未填写企业时兼容服务端原有 `budget_corp_id` 等别名，
  不猜测企业；新开关默认关闭，手动、兼容及定时台账同步均检查完整配置和启用状态。
- `budget` 连接来源复用 `budget_db_*` 及现有别名；`shared_oa` 复用
  `overseas_costing_oa_db_*`。必须显式配置 host/user/password；库名默认 `dingtalk_oa`，
  端口默认 5432。共享来源同时传递已有 SSL 模式。密码只在服务端使用，设置 API 不返回连接凭据。
- 同步、组织树和审批统计读取使用同一企业及连接；同步查询同时限定企业和预算模板。
  未配置时公司/部门选项退回本地配置及台账，返回明确状态，不弹服务器错误。
- 实际支出及审批中支出仍来自独立的历史汇总库，该库没有逐行企业隔离字段。
  本地模式读取前核对原服务端企业配置、主机和端口与台账来源一致；不一致时停止支出查询，
  不猜测新库名，也不把数据缺失当作零支出。详情返回 `expense_available=false`，
  支出和余额为 `null`；看板返回分项 `availability`，保留预算数据，隐藏不可用的支出指标和图表。
  停用同步不影响来源匹配后的只读查询。显式 HTTP/PostgreSQL 模式保留原行为。
- 同步窗口跟踪 RQ 实际状态和持久运行日志，区分排队、运行、成功、失败和跳过，
  展示新增/更新等数量；跳过的已启动任务也保存 `run_id` 和 `reason_code`，RQ 结果过期后
  仍能显示跳过原因。停用状态下的日常定时检查不新增无意义日志。
  关闭窗口停止轮询，打开窗口不会自动提交同步任务。
- 保存设置与同步共用站点锁，包括原生设置表单；原生保存从写库前持锁到事务提交或回滚，
  避免首次导入与来源切换并发。已排队任务执行前复核配置版本。成功同步将来源指纹
  与台账同事务保存，台账已有数据时阻止直接切换企业或数据库；服务端连接变更后指纹
  不匹配也停止同步，避免跨来源覆盖或删除历史数据。

实现入口：`services/ledger_sync_settings.py`、`services/oa_budget_sync.py`、
`public/js/budget_ledger_sync.js`。更新站点需同步 `Budget Settings`、`Budget Ledger Sync Log`
新增字段和清理缓存；
未配置的数据源保持停用，不能把本地界面测试视为 OA 实际同步验收。

显式 `http` 模式只需配置 `dingtalk-oa` 的地址与企业范围，**不需要数据库连接**；本地预算台账同步使用上节独立配置：

```
BUDGET_DATA_SOURCE     # local（默认）| http | postgres
BUDGET_READ_API_URL    # dingtalk-oa 地址，如 https://oa.example.com
BUDGET_READ_API_TIMEOUT# 秒，默认 30
BUDGET_CORP_ID         # 本实例负责的企业，必填
```

`BUDGET_READ_API_URL` 兼容既有的 `BUDGET_OA_API_URL` / `operating_expense_oa_api_url` /
`budget_oa_api_base`，避免同一站点配两处地址。旧开关 `BUDGET_OA_SOURCE` 仍生效，
但优先级低于 `BUDGET_DATA_SOURCE`。

`BUDGET_CORP_ID` 是查询范围，不是可选优化：OA 库多企业共库，`ding_department_tree`
唯一键是 `(corp_id, dept_id)`，`businessId` 也可能跨企业重复。未配置时部门树查询直接
报错，不会退化成读全库。企业范围兼容 `budget_oa_corp_id` / `dingtalk_oa_corp_id` /
`dingtalk_corp_id`。

所有数据源统一由 `integrations/budget_factory.py` 与 `oa_factory.py` 选择：

| 工厂函数 | http（显式配置） | postgres（应急回退） |
| --- | --- | --- |
| `get_ledger_source()` | `BudgetLedgerAPISource` | `BudgetLedgerSource` |
| `get_quota_source()` | `QuotaAPISource` | `QuotaSource` |
| `get_expense_source()` | `ExpenseAPISource` | `ExpenseSource` |
| `get_budget_expense_source()` | `BudgetExpenseAPISource` | `BudgetExpenseSource` |
| `get_oa_source()` | `OAAPISource` | `OAApprovalSource` |

两组实现的方法签名与行字段完全一致，业务层不感知来源。

> 注意：台账同步后落在**站点自己的 MariaDB**（`Budget Ledger Entry`），
> `local` 模式的列表、筛选、排序、导出走站点库；组织选项可从配置完整的 OA 只读库读取，台账同步必须单独启用。

### 应急回退到 PostgreSQL

`BUDGET_DATA_SOURCE=postgres` 时恢复本仓库原有的 psycopg3 直连实现，此时才需要：

```
BUDGET_DB_HOST / BUDGET_DB_PORT / BUDGET_DB_NAME
BUDGET_DB_USER / BUDGET_DB_PASSWORD
```

库名按「数据源自己的键 → `BUDGET_DB_NAME` → 该数据源默认库名」解析：

| 数据源 | 库名键（可选） | 未配置时的默认 |
| --- | --- | --- |
| OA 审批 / 部门树 | —（直接用 `BUDGET_DB_NAME`） | `dingtalk_oa` |
| 预算台账 | `BUDGET_LEDGER_DB_NAME`（旧：`budget_system_db_name`） | `budget_system` |
| 审批支出 | `BUDGET_APPROVAL_DB_NAME`（旧：`dingtalk_approval_db_name`） | `dingtalk_approval` |

兼容既有键名 `budget_oa_db_*` / `dingtalk_oa_db_*` / `oa_mapping_pg_*`；
OA 连接一律 `default_transaction_read_only=on` + `statement_timeout`，账号请只授只读权限。

### dingtalk-oa 侧

新增预算只读接口后，`dingtalk-oa` 需要能读到预算库；主机与账号留空时复用它的主 PG 连接：

```
BUDGET_DB_HOST / BUDGET_DB_PORT / BUDGET_DB_USER / BUDGET_DB_PASSWORD
BUDGET_DB_NAME                  # 默认 budget_system
BUDGET_APPROVAL_DB_NAME         # 默认 dingtalk_approval
BUDGET_API_REQUESTS_PER_MINUTE  # 默认 120
BUDGET_API_STATEMENT_TIMEOUT_MS # 默认 15000
```

钉钉凭据（`frappe.conf` 或环境变量，`DingTalkConfig.from_site_config()`）：

```
dingtalk_app_key / dingtalk_app_secret
dingtalk_robot_app_key / dingtalk_robot_app_secret / dingtalk_robot_code
```

## 台账同步

数据链路：钉钉 OA 归档（PostgreSQL，只读）→ `parse_budget_instance` 解析 → 按
(dept, 预算月份) 去重取最新 → `Budget Ledger Entry` / `Budget Ledger Detail`（本地 MariaDB）。
页面此后只读本地表，外部 PG 只在同步时使用。

### 三个 DocType

| DocType | 作用 |
| --- | --- |
| `Budget Ledger Entry` | 台账主表，`autoname = form_no`；`source_digest` 存该单指纹，增量判定用 |
| `Budget Ledger Detail` | 明细，按 `form_no` 归属；某一单变化时只重建这一单的明细 |
| `Budget Ledger Sync Log` | 每轮一条运行记录（`Running / Success / Failed / Skipped`），只读，30 天后自动清理 |

### 触发方式

| 入口 | 用途 |
| --- | --- |
| `scheduled_sync()` | 外部调度器（cron / ofelia / launchd），`trigger=Scheduled`，失败抛异常以拿到非零退出码 |
| `run_oa_budget_sync(force=0)` | 手动同步；`force=1` 忽略指纹整表重建 |
| `enqueue_oa_budget_sync()` | 入队 long 队列，`job_id + deduplicate` 保证不会重复排队 |
| `ensure_ledger_fresh()` | 台账列表页打开时调用，数据超过 120s 未更新才入队；60s 内试过则退避 |
| `hooks.py` `daily_long` | `sync_budget_entries` 转调 `scheduled_sync`，站点开着 scheduler 时生效 |

站点 `enable_scheduler: 0` 时前四项之外都没人触发，需要外部定时器，例如：

```bash
bench --site <site> execute budget.services.oa_budget_sync.scheduled_sync
```

### 增量与全量

- **默认增量**：每单算 `sha256(最终入库字段 + 明细)`，与本地 `source_digest` 相同就整单跳过
  （不 upsert、不碰明细）。没人改单时每轮 0 写入。
- **每日整表对账**：`scheduled_sync` 发现距上次成功全量满 24h，自动把这一轮切成全量，
  对冲「本地被手工改坏」这类增量发现不了的问题。
- **校验失败自动升级**：增量轮写入后 `_verify` 发现条数/金额/明细对不上，会立刻以全量重跑一次，
  修好才提交；全量仍对不上才判定失败并整批回滚。
- **原子性**：全部写库 → 校验 → 提交；任一步失败 `rollback`，本地保持上一轮完整快照。

### 并发、超时与可观测性

- Redis 锁 `budget-ledger-sync`（`SET NX` + token 释放）串行化多 worker / 定时 / 页面触发；
  拿不到锁的那轮记 `Skipped`，不写库。
- 单轮时间预算默认 300s，站点配置 `budget_ledger_sync_timeout` 可改；超时抛
  `LedgerSyncTimeout` 并整批回滚。
- 最近的同步结果用 `get_ledger_sync_status()` 查（返回 `last` / `recent` / `local` / `lock_held`），
  数据来自 `Budget Ledger Sync Log`，Redis 重启不丢。

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
