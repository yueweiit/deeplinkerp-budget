# 审批对接 · 时序与数据流

本文只讲「请求进来之后发生了什么」和「增量怎么续」。字段规则的解释见
[README.md](README.md)，上游契约见 `dingtalk-oa/docs/modules/approval-read-api/README.md`。

## 1. 只读查询（已实现）

```text
前端 / 其它 app
      │  frappe.call("budget.api.approval.get_approval_instance", ...)
      ▼
api/approval.py
      │ 1. frappe.only_for(("System Manager", "Accounts Manager"))
      │ 2. 取请求级数据源 frappe.local.budget_approval_source
      ▼
services/approval_query_service.get_approval_instance()
      │
      ├── integrations/oa_approval_source.get_instance_row(instance_id, with_snapshot=True)
      │       └── SELECT ... LEFT JOIN LATERAL ding_user_snapshot（补 snapshot_user_name）
      │           条件：corp_id = %s AND (process_instance_id = %s
      │                 OR raw_payload->>'businessId' = %s)
      │                 ← businessId 跨企业可能重复，必须带企业范围
      │
      ├── services/approval_attribution_service.build_instance_detail(row)
      │       └── raw_payload 优先，缺失回落行字段；产出 camelCase
      │
      └── apply_originator_snapshot(detail, row["snapshot_user_name"])
              └── 姓名归因（见第 3 节）
      ▼
返回 dict 或 None（不存在不抛错，交给调用方判断）
```

一次请求内只建一个 `OAApprovalSource`（`frappe.local`），底层连接按需新建、用完即关，不做连接池。

## 2. 增量拉取（HTTP）

```text
读取上一轮完成水位 watermark（支出同步已保存于本地快照/模板检查点）
      │
      ▼
updatedAfter = compute_next_watermark(watermark)   # 自动回退 10 分钟
      │
      ▼
OAReadAPIClient.iter_approval_pages(corp_id, process_code=..., updated_after=updatedAfter)
      │
      ├── GET /api/v1/approvals?corpId=&updatedAfter=&limit=50
      ├── 429 ──────────► 按 Retry-After 退避后重试（max_retries）
      ├── 400 INVALID_CURSOR ──► 抛 OAInvalidCursor，调用方丢弃游标重开
      └── 200 ──────────► ApprovalPage(rows, next_cursor, sync_until, has_more)
      │
      ▼
整轮成功后，才把最后一页的 sync_until 存为新水位
（不要存「某页的最大 updated_at」，也绝不存 last_event_time）
```

支出工作台已将同一协议接入每 5 分钟调度，并每 24 小时全量核对。CN 持久化和原子发布入口为 `budget/services/cn_approval_sync.py`；MX 分页入口为品牌 App 的 `services/mexico_expenses.py`。具体边界见 [Budget README](../../../README.md#权限与配置) 和品牌 App 的 `docs/mexico-expenses.md`；上面的 `OAReadAPIClient` 是其他审批调用方使用的客户端，支出同步复用 `operating_oa_http.read_template_page`。

为什么必须重叠：时间水位不是事务提交序号，长事务可能在你翻页之后才提交。
下一轮用 `sync_until - 10min` 重读一小段，按主键幂等覆盖，比漏数便宜得多。

## 3. 发起人姓名归因

```text
source_name = raw_payload.originatorUserName ?? row.originator_user_name
user_id     = raw_payload.originatorUserId ?? row.originator_user_id

source 为空 或 等于 user_id 或 是纯 ASCII 数字（工号被误存成姓名）
        ├── 是 ──► 取 snapshot_user_name，没有则用 source
        └── 否 ──► 用 source，没有则用 snapshot_user_name
                        │
                        └── 两者都空 ──► 兜底 user_id
```

全角数字（如 `１２３`）不算工号，原样保留——与上游 `^\d+$` 的 ASCII 语义一致。
只有走 Q5（`with_snapshot=True`）才做这层归因；`with_snapshot=False` 是 budget 原口径，不做补名。

## 4. 部门：三层选项与唯一反查

**三层选项**（`list_organization_options`）

```text
ding_department_tree (corp_id = 本实例企业 AND is_current = true)
      │ 每行带 path_ids / path_names
      ▼
在 path_names 里找集团根名（yuewei / 悦为集团 / 悦为集团yuewei grupo）
      ├── 找不到 ──► 丢弃（例如外部组织）
      ├── 集团 = path[group_index]
      ├── 公司 = path[group_index + 1]（不存在则只出集团）
      └── 部门 = 自身 dept_id（必须比公司更深一层）
      ▼
groups / companies / departments 三组，按名称排序
```

**唯一反查**（`resolve_department`）

```text
候选行（按 dept_code / 对应部门+路径 / 业务主体名 三种匹配之一）
      ▼
仅保留 is_current = true 且 dept_id、name 都非空
      ▼
按 dept_id 去重后必须恰好 1 条
      ├── 0 条 ──► None
      └── ≥2 条 ─► None（不猜、不按名称兜底）
```

## 5. 部门路径快照（Q9）

```text
list_department_snapshot_rows(dept_ids)
      │  一条 SQL 内完成两段筛选：
      ├── current_matches：is_current = true 且该 dept_id 只有 1 条，路径数组非空
      └── historical_matches：没有当前行的 dept_id 中，历史行恰好 1 条
      ▼
normalize_department_snapshots(rows)
      └── 名称 / path_ids / path_names 任一为空则丢弃
      ▼
{dept_id: {department, path_ids, path_names}}
```

注意：部门树唯一键是 `(corp_id, dept_id)`，同步为 upsert，所以同一企业内「同一 dept_id 多条当前行」
不会发生；历史分支只对**已下线部门**生效。`dept_id` 只在企业内唯一，SQL 的 candidates CTE
必须先按 `corp_id` 收敛，否则第二家企业的同名 `dept_id` 会被算成「多条匹配」而误判为不可用。

## 6. 失败路径

| 场景 | 表现 | 处理 |
| --- | --- | --- |
| 实例不存在 | `get_approval_instance` 返回 `None` | 调用方判断，不抛错 |
| 上游 429 | `OARateLimited(retry_after)` | 退避重试；跨轮次仍失败则记日志并跳过本轮 |
| 游标失效 | `OAInvalidCursor` | 从上次完成水位整轮重开 |
| 库连不上 | psycopg 异常向上抛 | 不吞异常，由 api 层记 `frappe.log_error` |
| 熔断打开 | `CircuitOpenError` | 立即失败，等窗口恢复，不要重试 |

## 7. 尚未落地

```text
[未实现] 水位持久化     → 需要同步账本 DocType（sync_until / cursor / 状态机）
[未实现] 定时调度       → hooks.py 的 scheduler_events，当前留空避免挂未实现函数
[未实现] 幂等写入       → payload_hash + 主键覆盖
[未实现] UNCERTAIN 回读 → 上游超时不代表没收到，需回读对账
[未实现] 附件           → 上游 /api/v1/attachments + /api/v1/attachment-content
```
