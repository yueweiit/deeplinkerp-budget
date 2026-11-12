# 审批对接模块（approval connector）

**结论先行**：本模块是**只读消费方**。钉钉数据由上游 `yueweiit/dingtalk-oa` 拉取入库，
本模块只做三件事——从上游 HTTP API / 只读库取数、按业务规则归因、通过只读接口对外提供。
上游缺能力时**给上游提 PR**，不在 Python 侧复制一套拉取逻辑。

- 代码入口：`budget/api/approval.py`（对外）、`budget/services/`（判定）、`budget/integrations/`（IO）
- 上游契约：`dingtalk-oa/docs/modules/approval-read-api/README.md`
- 上游表结构：`dingtalk-oa/migrations/*.cjs`

## 1. 职责边界

| 做 | 不做 |
| --- | --- |
| 读取审批快照、部门树、用户快照、任务明细 | 调钉钉接口拉数据（上游负责） |
| 姓名归因、部门分层、唯一性判定、水位换算 | 写 `dingtalk_oa` 库（上游是唯一写方） |
| 对外暴露只读接口 | 建预算单据、改 ERPNext 上游代码 |
| 组装 camelCase 详情结构 | 附件下载（`/api/v1/attachments` 尚未接入） |

## 2. 数据从哪里来

| 需要的数据 | 通道 | 代码 |
| --- | --- | --- |
| 审批列表 / 增量 | 上游 HTTP `/api/v1/approvals` | `integrations/oa_read_api_client.py:97` |
| 审批详情 | 只读库（`processInstanceId` 或 `businessId`） | `integrations/oa_approval_source.py:135` |
| 部门树 / 用户快照 | 只读库 | `oa_approval_source.py:197`、`:247` |
| 部门按业务主体反查 | 只读库（必须唯一命中） | `oa_approval_source.py:208` |
| 审批效率（实例 + 任务） | 只读库 | `oa_approval_source.py` 的 `list_approval_efficiency_rows` |
| 预警消息 | 钉钉机器人（仅此一处直连钉钉） | `integrations/dingtalk_client.py:315` |

### 两条硬约束

1. **不要用 `last_event_time` 当时间水位**。数据字典写明它是「规范化时的本地刷新时间，并非严格的上游事件时间」，
   每次本地重新规范化都会刷新它，用它做窗口会重复或漏数据。可用的是 `updated_at`
   （有触发器维护：源内容不变时保留旧值）。
2. **不要用 `OFFSET` 分页**。库在持续 upsert，翻页期间的插入会把后续行挤出已翻过的区间，造成静默漏数。
   上游 API 已经提供 `(updated_at, process_instance_id)` 键集游标，直接用它。

## 3. 分层

```
api/approval.py            参数入口 + 权限 + 透传，不写判定
      ↓
services/                  纯判定：归因、分层、唯一性、水位
      ↓
integrations/              钉钉 HTTP、上游只读 API、OA 只读库（psycopg3）
```

| 文件 | 职责 | 是否可脱 Frappe |
| --- | --- | --- |
| `services/approval_attribution_service.py` | 姓名归因、详情组装（camelCase） | 是 |
| `services/organization_service.py` | 集团/公司/部门三层、唯一命中、路径快照归一 | 是 |
| `services/approval_watermark_service.py` | `syncUntil` → 下一轮 `updatedAfter`（回退 10 分钟） | 是 |
| `services/approval_query_service.py` | 编排：调 integrations + 套用上面三层判定 | 是 |
| `integrations/oa_read_api_client.py` | 上游 HTTP：cursor 分页、429 退避、`INVALID_CURSOR` | 是（注入 session） |
| `integrations/oa_approval_source.py` | OA 只读库查询（只读事务 + 语句超时） | 是（注入 connect） |
| `integrations/dingtalk_client.py` | token 缓存、机器人发送、三组熔断 | 是（注入 session/cache） |

## 4. 对外接口

全部 `@frappe.whitelist()`，权限统一走 `_require_read_permission()`（`System Manager` / `Accounts Manager`）。

| 接口 | 用途 | 数据来源 |
| --- | --- | --- |
| `get_approval_instance(instance_id, with_snapshot=True)` | 按实例 ID 或 businessId 取详情 | 库 + 归因 |
| `list_organization_options()` | 集团 / 公司 / 部门 三层 | 库 |
| `get_department_by_service_entity(service_entity, ...)` | 反查唯一部门，不唯一返回 `None` | 库 |
| `list_department_snapshots(dept_ids)` | 部门路径快照（当前唯一优先，历史回退） | 库 |
| `get_approval_efficiency(start_date, end_date)` | 实例 + 任务两张明细 | 库 |

命名契约：`get_` / `list_` 表示**无副作用**，前端可安全重试；写接口后续单独成模块。

数据源实例按请求复用（`frappe.local.budget_approval_source`），连接不池化，用完即关。

## 5. 配置

| 用途 | 键（环境变量优先，其次 `frappe.conf`） | 默认 |
| --- | --- | --- |
| 上游 API 地址 | `budget_oa_api_url` / `operating_expense_oa_api_url` | 必填 |
| 上游 API 超时 | `budget_oa_api_timeout` | 30 秒 |
| OA 库连接 | `budget_oa_db_host` / `_port` / `_name` / `_user` / `_password` | `localhost:5432/dingtalk_oa` |
| 企业范围 | `budget_oa_corp_id` | 必填（缺失时查询报错） |
| 兼容旧键 | `dingtalk_oa_db_*`、`oa_mapping_pg_*`、`dingtalk_oa_corp_id` / `dingtalk_corp_id` | — |
| 钉钉应用 | `dingtalk_app_key` / `dingtalk_app_secret` | — |
| 机器人 | `dingtalk_robot_app_key` / `_secret` / `_code` | — |

OA 库连接强制 `default_transaction_read_only=on` + `statement_timeout`，账号请只授 `SELECT`。

## 6. 错误与重试

| 异常 | 触发 | 调用方处理 |
| --- | --- | --- |
| `OAInvalidCursor` | 游标过期/篡改/筛选条件变化 | 丢弃游标，从**上次完成水位**重开一轮 |
| `OARateLimited` | 上游按来源 IP 限流（默认 120 req/min） | 按 `retry_after` 退避；跨来源共享 NAT 额度 |
| `OAReadAPIError` | 其它 4xx/5xx，`code` 为稳定错误码 | 记 `frappe.log_error`，按 code 决定是否重试 |
| `DingTalkAPIError` | 钉钉 token / 机器人失败 | 同上 |
| `CircuitOpenError` | 熔断打开（token 3 次/30s、其它 5 次/60s） | 不要重试，等窗口恢复 |

## 7. 已知约束

- **上游只读 API 无鉴权**，安全边界只靠网络与反向代理；不要把地址暴露到公网。
- **附件与正文未接入**：需要时走上游 `/api/v1/attachments`，按 `contentQuality` 判断是否预览件。
- **部门树无多版本**：唯一键 `(corp_id, dept_id)`，同步是 upsert，历史回退只对已下线部门有效。
- **查询必须带企业范围**：`ding_department_tree` / `ding_approval_instance` 等表是多企业共库，
  `OAApprovalSource` 的每条 SQL 都以 `corp_id` 过滤；配置缺失时抛 `OAApprovalSourceScopeError`，
  不允许退化成全库查询。
- **水位的重叠区间不是可选项**：上游建议至少 10 分钟，`approval_watermark_service` 已内置默认值。

## 8. 测试

```bash
cd apps/budget
PYTHONPATH=. pytest -q budget/tests
```

覆盖点：归因六种分支、分层与唯一命中、水位回退与格式校验、HTTP 分页/限流/游标失效、
只读会话参数、熔断开关、机器人部分失败。全部通过注入替身运行，不需要站点、数据库或网络。

## 9. 未实现（第二阶段）

同步账本 DocType（`PENDING / RUNNING / SUCCESS / FAILED / UNCERTAIN / MANUAL_REQUIRED`
+ `payload_hash` 幂等），配套 `*_ledger_service.py` 负责落库、`hooks.py` 登记 cron、
`UNCERTAIN` 靠回读上游 reconcile。
