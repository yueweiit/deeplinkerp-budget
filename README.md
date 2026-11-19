# budget

预算与费用审批 app：把 `dingtalk-budget`（Express）与 `dingtalk-expense-sync`（TypeScript）
两个 Node 项目重写成一套 Python 实现，按 DeepLinkERP 的分层纪律组织：

> **命名**：app 按业务域命名（对齐 `china_finance` / `overseas_costing` / `mes_integration`），
> 不按数据源命名。钉钉与 `dingtalk_oa` 库只是数据来源，不是这个 app 的边界。

导航入口为“运营支出（CN）”（`China Operating Expenses`）。独立安装保留预算台账、预算看板和预算额度入口；与 `deeplinkerp_branding` 联合安装时整合进 CN 工作台。Sidebar、Desktop Icon 及每个 Page/DocType 仍按原生权限过滤，不额外授权业务数据。

安装新版 `deeplinkerp_branding` 后，CN 提供支出概览、支出明细、统计报表和数据核对，取消独立“预算管理”分组。选择上方“预算”后，概览、明细及核对读取本地预算；明细提供“预算台账／预算额度”页签。原预算看板归入“统计报表 → 预算执行分析”。运营支出仍复用中国财务原申请、付款及办理接口；采购及薪酬统计尚未接入，不读取 MX 数据或模拟金额。详细运营支出口径由品牌 App 的 `docs/operating-expenses-implementation.md` 维护。

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

联合安装下，“预算执行分析”沿用 `/desk/budget-dashboard`；旧 `/desk/budget-config` 转到预算明细的额度页签，`/desk/budget-ledger-entry` 仍可直接打开原生列表，原单详情保留。CN 集成时列表、报表、详情及额度均读取同步后的本地数据，旧 `budget_data_source` / `budget_oa_source` 不再切换 CN 来源或限制导航；独立 Budget 安装仍保留原模式及原组件。`after_install` / `after_migrate` 调用 `budget.setup.apply_budget_nav` 重放导航，不更改 ERPNext 原生 Budget 菜单及业务数据。

### CN 预算整合（2026-11-19）

- `budget/api/cn_workbench.py` 提供本地预算概览、分页明细、核对及最多 10000 行的 XLSX 导出。日期按覆盖的完整预算月份归一；公司取来源原名精确筛选，不与法律公司做模糊合并。预算申请按台账 `COALESCE(total_amount, amount, 0)` 计，真实零值保留。
- 概览与台账按预算月份查询；核对覆盖公司/部门/月份缺失及金额差异（超过 0.01）。公司层级预算允许部门展示名为空，须有相符的组织路径，或由组织映射明确核对为公司层级；不能仅凭非空组织 ID 跳过核对。缺少预算月份的核对记录按申请日期定位，不计入已归期预算金额。概览待核对数采用同一核对查询。
- 预算额度复用 `BudgetQuotaLocalSource`，只计当前配置企业及预算模板的 `effective`，按公司及月份区间读取；下拉候选、统计卡片和导出使用相同范围，其他企业历史额度保留但不混入当前统计；列表、汇总和导出共用部门 ID／名称下钻条件。保留集团、公司、部门、科目汇总以及每单科目明细。额度和预算申请分别展示，不相加，也不混入实际支出。共用 OA 来源的“同步预算数据”使用同一次审批分页结果，在同一事务内更新台账和额度；不再查询独立预算库的 `budget_effective_quota` / `budget_effective_quota_subject`。
- `public/js/cn_budget.js` 为 CN 提供预算适配；品牌 App 负责公共日期、导航及生命周期。原报表和详情分别提取为 `public/js/budget_report.js`、`budget_detail.js`，原生页面和 CN 共用。预算仅用人民币，日期/公司及额度页签随导航保留。
- 额度同步由 `services/oa_budget_quota.py` 复用现有预算表单解析和组织映射，按企业、预算模板和国内执行地区限定来源；同部门、预算月份采用台账的最新申请规则。额度保留原 `COMPLETED/TERMINATED + agree` 兼容口径，台账仍只纳入 `COMPLETED + agree`；表头金额作为额度，科目用 Decimal 按类型和名称汇总，金额差异单独标记。缺失月份、身份冲突或分页不完整会整批失败并回滚；完整空结果保留原数据；完整非空快照确认已无符合条件的台账审批时，按既有快照机制移除失效的派生台账及明细，并核验条数和金额归零，同一事务更新额度，失败则整批回滚。被新申请替代、撤回或不再符合条件的额度标记 `deactivated`，保留历史单据和科目；不删除其他企业的额度。
- `quota_sync.run_quota_sync/enqueue_quota_sync/sync_quotas` 保留兼容入口，共用 OA 模式委托预算同步任务，沿用启用开关、配置版本、来源绑定和同一个互斥锁；手动入口要求管理员 POST。原每日独立额度任务已从 hooks 移除，避免重复扫描；已登记的旧任务也只会走上述兼容入口。非共用来源返回 `shared_oa_required`，不会回退独立预算库。页面和 API 继续读取本地 `Budget Quota` / `Budget Quota Subject`，不新增字段、不自动启用同步。独立安装的显式旧读取适配器保留兼容，不属于 CN 同步链路。
- 报表公司筛选同时约束预算与支出。支出只通过可靠、唯一的部门 ID 归属公司；归属不完整或冲突时，公司执行金额显示不可用，不把全公司金额或零值作为该公司金额。独立旧模式的在途支出和审批效率接口没有可靠公司维度，选择公司时相应指标显示不可用；同年趋势仍显示自然年。
- CN 共用来源的“预算执行分析”通过 `integrations/cn_expense_report_source.py` 读取品牌 App 的本地运营审批快照，沿用原生列表/公司/字段权限，不再调用旧独立支出库和审批库。**已审批支出**按审批完成日期（中国时区）归期，审批中金额按申请日期归期；外币复用 MX 的月初汇率服务折算 CNY，分别采用审批完成月及申请月。只覆盖已接入的国内运营申请，采购尚未纳入；审批通过不代表付款，预算申请也不计入支出。页面卡片及图例明确使用“已审批支出”，口径放入悬停提示。
- 预算组织以相同企业下的组织 ID 精确关联；来源主体与发起人部门不同则不能借用发起人部门 ID。未关联记录仍计入全部公司支出并单列，不能用相似名称冲抵预算。公司筛选通过台账组织归属及显式组织映射约束金额与审批效率。审批效率按区间内发起的申请和 OA 任务时间计算；缺少完成时间、汇率或必要节点信息时返回可翻译的不可用状态，预算部分仍可展示；预算总额为零时整体执行率为 `null`（页面“—”）。
- 品牌 App 同步保存 `workflow_summary.reporting` 的精简时间快照；已有申请在正常同步结束核查时补齐。读取报表不会主动同步审批或生成付款记录，无需新增数据库字段。接口保留 `approvedTotal`、`actualExpense` 等历史字段，新增 `expense_basis=approved` 和 `scope_notice` 区分展示口径，独立旧模式保持兼容。
- 执行状态图按预算、已审批支出和审批中支出的组织并集汇总；没有预算或未关联预算组织的已审批金额仍单列展示。全年趋势读取失败时，重新按当前查询区间计算金额与执行率，返回独立的 `availability.trend_expenses=false`，趋势中的支出值为 `null`，前端只隐藏该趋势系列；本期也无法核实的金额仍显示不可用。OA 来源初始化失败时保留本地预算，显示一条翻译后的配置提示，不回退到旧来源，也不附带服务器错误弹窗。这些读取均不修改同步配置或业务数据。
- 台账、概览、核对、报表要求 `System Manager` / `Accounts Manager`；额度与同步设置仍仅允许 `System Manager`。前端隐藏额度页签之外，后端每次读取/导出均鉴权；未扩大角色授权。全部审批表仍是运营支出合计，预算单独展示。
- CN 预算浏览不触发同步；同步设置及同步执行复用 `BudgetLedgerSync`。未配置连接/企业范围时仍不得启用或同步。
- 同步设置新增 `operating_oa`（共用 CN/MX OA 审批接口），由 `integrations/shared_operating_oa.py` 复用品牌 App 的只读审批客户端与服务器端 URL／企业白名单，按预算模板、审批通过状态和国内执行地区读取。单企业无需重复填写企业 ID，多企业必须明确选择；未配置且无历史台账的 CN 站点默认展示此来源，但不会自动启用或同步。已绑定台账仍禁止直接换企业/来源，已有历史记录不会因读取页面而重写。
- HTTP 同步逐页检查身份、快照、重复记录、游标前进和页数上限，读取完整后才复用原事务、幂等及金额核验写入本地。现有共享接口没有部门树端点，保留已确认的本地组织路径，新组织保留 OA 原名，由管理员在明细／核对行的“核对组织归属”中明确指定公司、部门或公司层级；不猜测公司，不请求不存在的旧预算部门接口。共享接口未提供的审批统计和未关联的旧支出汇总显示不可用，不以零值代替。
- `services/organization_mapping.py` 将组织归属存入 `Budget Settings.organization_mappings`（子表 `Budget Organization Mapping`），按 `corp_id + department_id` 精确匹配，拒绝重复、跨企业、缺失名称及层级冲突。同步设置可集中维护；保存与同步共用锁，原生设置保存也校验并记版本。保存映射在同一事务内更新对应台账的公司／部门及 `reviewed_organization_level`，并更新当前企业、预算模板额度的公司／部门、公司 ID 和组织核对标记；清空台账旧摘要以便后续同步重检，保留 OA 原名、原路径和全部金额。删除映射恢复原路径推导结果；无原路径时使用同单台账保留的 OA 名称，无可核实原名则清空部门展示并回到待核对，不保留旧映射名称。
- 已有台账或额度（含仅有历史额度的站点）通过同步设置“验证并切换 OA 来源”迁移到 `operating_oa`：`services/ledger_source_transition.py` 检查同企业白名单，完整读取国内预算审批，按台账和额度各自的审批条件逐笔匹配实例 ID、单号、组织 ID、月份、类型、模板及金额，并核对额度科目汇总。其他企业及模板额度不参与切换核验，也不改写。预览仅生成绑定当前用户／配置／数据摘要的 10 分钟凭据；确认时在同步锁内重新核对，并锁定现有台账、当前范围额度及科目行。验证失败、超时、企业不符或预览后变化均拒绝切换。仅这条受控路径允许重新绑定指纹，保留台账、额度及组织映射，记录审计说明并停用同步；不会导入、删除数据或自动入队。历史非国内记录、缺少实例 ID 或金额不一致须先人工查明，不能靠清空指纹绕过。
- 发布须先同步 `Budget Settings`、新增子表 `Budget Organization Mapping` 及台账 `reviewed_organization_level` 元数据，再启用新版页面。无需回填或删除历史台账。未迁移站点按实际列存在情况读取旧组织路径，概览、明细、核对与导出仍可用；界面明确提示组织归属核对尚未启用，并阻止映射保存、来源切换及同步写入，返回 `schema_update_required`。结构检查同时核对物理表／列与 Frappe 元数据，避免仅更新代码就查询不存在的列。当前已在未迁移开发站点只读调用概览、明细、核对、导出和设置接口，并通过浏览器验证概览返回 HTTP 200、四张统计卡片正常渲染且无页面脚本异常；台账与同步日志数量保持不变。未运行迁移、真实同步或来源切换。

验证入口：`budget/tests/test_cn_workbench.py`、`test_cn_navigation.py`、`test_organization_mapping.py`、`test_ledger_source_transition.py`、`cn-budget.test.js`、`ledger-sync.test.js`、`dashboard-availability.test.js`；额度来源与事务回归见 `test_oa_budget_quota.py`、`test_shared_operating_oa.py`。开发站点另用事务内合成记录验证公司隔离、跨月、缺失月份核对、分页和导出，完成后回滚；浏览器验证 CN/原入口切换及无自动同步。此验证不等于生产数据核对或部署。

## 配置

### 预算台账同步（2026-11-19）

台账当前默认使用 `local`（见 `budget/integrations/budget_factory.py`），由
`services/oa_budget_sync.py` 按连接配置只读读取 OA PostgreSQL 归档或共用 OA 审批接口，并解析到本地台账。
下面的旧 HTTP 适配器与 `operating_oa` 共用审批接口是两个独立入口；CN 集成仅展示本地快照。

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
